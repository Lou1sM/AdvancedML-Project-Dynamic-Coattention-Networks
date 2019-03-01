import tensorflow as tf
import highway_max_out
import encoder
import ciprian_data_prep_script
import argparse
import numpy as np


parser = argparse.ArgumentParser()
parser.add_argument("--num_epochs", default=1, type=int, help="Number of epochs to train for")
parser.add_argument("--restore", action="store_true", default=False, help="Whether to restore weights from previous run")
parser.add_argument("--lstm_size", default=200, type=int, help="Number of recurrent units for the first lstm, which is deteriministic and is only used in both training and testing")
parser.add_argument("--test", "-t", default=False, action="store_true", help="Whether to run in test mode")
parser.add_argument("--batch_size", default=32, type=int, help="Size of each training batch")
parser.add_argument("--dataset", choices=["SQuAD"],default="SQuAD", type=str, help="Dataset to train and evaluate on")
ARGS = parser.parse_args()

hyper = {'num_units':128, 'keep_prob': 1, 'batch_size': 1}

# Batches are assumed to span the first dimension of tensors
# The first start- and end-pointers are assumed to be the start
# and end of document (we might consider randomising these)

# Global variables
# We may want to move these to an object and pass it to components

# Learning Parameters
num_epochs = 100
num_batches = 64
learning_rate = 1e-3
dropout = 0.3
# The authors "apply 0.3 dropout on the question and document encodings"

# Architecture Parameters
hidden_state_size = 200

# Whatever the format of the data preparation, the output will
# be a document-length sequence of glove word vectors, and a
# question-length sequence of the same. Ideally it these would
# be generator objects with the faster tf.data.Dataset, but
# another format is ok if that's all that's possible
#
# For example:
input_d_vecs, input_q_vecs, ground_truth_labels = ciprian_data_prep_script.get_data()

# Expecting ground truth labels to be a tuple containing indices
# of start and end points
#print("d vecs type:", type(input_d_vecs))
dataset = tf.data.Dataset.from_tensor_slices(input_d_vecs)
dataset = dataset.batch(ARGS.batch_size)
iterator = dataset.make_initializable_iterator()

new_doc_vecs, new_q_vecs, new_ground_truth_labels = iterator.get_next()

# Encode into the matrix U, using notation from the paper
# The output should be of shape [2*hidden_size, document_length]
encoded = encoder.encoder(new_doc_vecs, new_q_vecs, hyper)

'''
# Testing code
encoded = tf.random.uniform([600, num_batches, 2*hidden_state_size])
new_ground_truth_labels = tf.random.uniform([2, num_batches, 600])
'''

# Create single nodes for labels
start_labels = tf.squeeze(tf.gather(new_ground_truth_labels, [0]), [0])
end_labels = tf.squeeze(tf.gather(new_ground_truth_labels, [1]), [0])

# Create and initialize decoding LSTM
decoding_lstm = tf.contrib.cudnn_rnn.CudnnLSTM(
    num_layers=1,
    num_units=hidden_state_size)

# Get lstm_dec hidden state values from U in order to make the
# first guess for start- and end-points
# Output should be of shape [2*hidden_size]
# Each guess is based on the previous guess so it seems we need
# to start with some random guess, eg first and last document words

u_s = tf.zeros([num_batches, 2*hidden_state_size])  # Dummy guess start point
u_e = tf.zeros([num_batches, 2*hidden_state_size])  # Dummy guess end point

for i in range(4):

    # LSTM input is concatenation of previous guesses
    usue = tf.concat([u_s, u_e], axis=1)

    # CudnnLSTM expects input with shape [time_len, batch_size, input_size]
    # As the inputs across time depend on the previous outputs, we simply set
    # time_len = 0 and repeat the calculation multiple times while setting
    # the 'initial' state to the previous output. CudnnLSTMs output is
    # similarly time-major (has a first dimension that spans time).

    # In the paper (and here), h is the concatenation of cell output and cell state
    # (h_t and C_t in colah.github.io/posts/2015-08-Understanding-LSTMs/ ).
    # The decoding_lstm returns time-major (h_t, (h_t, C_t)).
    # an initial_state of None corresponds to initialisation with zeroes
    with tf.variable_scope("decoding_lstm", reuse=tf.AUTO_REUSE):
        lstm_output, h = decoding_lstm(
            inputs=tf.expand_dims(input=usue, axis=0),
            initial_state=None if i == 0 else h,
            training=True)
        lstm_output_reshaped = tf.squeeze(lstm_output, [0])
    # the LSTM "state" in the paper is supposedly of size l. However, in reality
    # an LSTM passes both its "output" and "cell state" to the next iteration.
    # What exactly does the paper mean by h? If it's the concatenated cell state
    # and output, then each of those should be 1/2 hidden state size, which is weird.

    # Make an estimation of start- and end-points. Define the
    # set of graph nodes for the HMN twice in two different namespaces
    # so that two copies are created and they can be trained separately
    with tf.variable_scope('start_estimator'):
        alphas = highway_max_out.HMN(
            current_words=encoded,
            lstm_hidden_state=lstm_output_reshaped,
            prev_start_point_guess=u_s,
            prev_end_point_guess=u_e
        )
    alphas = tf.squeeze(tf.transpose(alphas), [0])

    with tf.variable_scope('end_estimator'):
        betas = highway_max_out.HMN(
            current_words=encoded,
            lstm_hidden_state=lstm_output_reshaped,
            prev_start_point_guess=u_s,
            prev_end_point_guess=u_e
        )
    betas = tf.squeeze(tf.transpose(betas), [0])

    batch_indices = tf.range(start=0, limit=num_batches, dtype=tf.int32)

    s = tf.argmax(alphas, axis=1, output_type=tf.int32)
    s_indices = tf.transpose(tf.stack([s, batch_indices]))
    u_s = tf.gather_nd(encoded, s_indices)

    e = tf.argmax(betas, axis=1, output_type=tf.int32)
    e_indices = tf.transpose(tf.stack([e, batch_indices]))
    u_e = tf.gather_nd(encoded, e_indices)

    # Define loss and optimizer, each guess contributes to loss,
    # even the very first
    s_loss = tf.nn.softmax_cross_entropy_with_logits_v2(
        labels=start_labels,
        logits=alphas
    )
    e_loss = tf.nn.softmax_cross_entropy_with_logits_v2(
        labels=end_labels,
        logits=betas
    )

    iteration_loss = s_loss + e_loss

    loss = iteration_loss if i == 0 else loss + iteration_loss

# Keep track of loss
tf.summary.scalar('loss', loss)

# Set up learning process
optimizer = tf.train.AdamOptimizer(learning_rate)  # They don't give learning rate!
train_step = optimizer.minimize(loss)

# For Tensorboard
merged = tf.summary.merge_all()
writer = tf.summary.FileWriter("summaries/")
writer.add_graph(tf.get_default_graph())


# Define the session, which will use default_graph, ie the
# graph that contains all the nodes defined in the current
# script (I think that's what default_graph is anyway)
with tf.Session() as sess:
    sess.run(tf.global_variables_initializer())

    # This bit will depend on data format, we can also
    # add summaries with 'writer' every so often, for
    # tensorboard loss visualization
    for i in range(num_epochs):
        for _ in range(num_batches):
            summary, _ = sess.run([merged, train_step])
        # currently write summary for each epoch
        writer.add_summary(summary, i)