import os
import time
import math
import json
import joblib
import random
import argparse
import numpy as np
import tensorflow as tf

from tqdm import tqdm
from functools import partial
from sklearn.utils import shuffle
from sklearn.metrics import accuracy_score

from copy import deepcopy

from opt import adam, warmup_cosine, warmup_linear, warmup_constant
from datasets import race
from text_utils import TextEncoder
from utils import encode_dataset, flatten, iter_data, find_trainable_variables, convert_gradient_to_tensor, shape_list, ResultLogger, assign_to_gpu, average_grads, make_path

def gelu(x):
    return 0.5*x*(1+tf.tanh(math.sqrt(2/math.pi)*(x+0.044715*tf.pow(x, 3))))

def swish(x):
    return x*tf.nn.sigmoid(x)

opt_fns = {
    'adam':adam,
}

act_fns = {
    'relu':tf.nn.relu,
    'swish':swish,
    'gelu':gelu
}

lr_schedules = {
    'warmup_cosine':warmup_cosine,
    'warmup_linear':warmup_linear,
    'warmup_constant':warmup_constant,
}

def _norm(x, g=None, b=None, e=1e-5, axis=[1]):
    u = tf.reduce_mean(x, axis=axis, keep_dims=True)
    s = tf.reduce_mean(tf.square(x-u), axis=axis, keep_dims=True)
    x = (x - u) * tf.rsqrt(s + e)
    if g is not None and b is not None:
        x = x*g + b
    return x

def norm(x, scope, axis=[-1]):
    with tf.variable_scope(scope):
        n_state = shape_list(x)[-1]
        g = tf.get_variable("g", [n_state], initializer=tf.constant_initializer(1))
        b = tf.get_variable("b", [n_state], initializer=tf.constant_initializer(0))
        return _norm(x, g, b, axis=axis)

def dropout(x, pdrop, train):
    if train and pdrop > 0:
        x = tf.nn.dropout(x, 1-pdrop)
    return x

def mask_attn_weights(w):
    n = shape_list(w)[-1]
    b = tf.matrix_band_part(tf.ones([n, n]), -1, 0)
    b = tf.reshape(b, [1, 1, n, n])
    w = w*b + -1e9*(1-b)
    return w

def _attn(q, k, v, train=False, scale=False):
    w = tf.matmul(q, k)

    if scale:
        n_state = shape_list(v)[-1]
        w = w*tf.rsqrt(tf.cast(n_state, tf.float32))

    w = mask_attn_weights(w)
    w = tf.nn.softmax(w)

    w = dropout(w, attn_pdrop, train)

    a = tf.matmul(w, v)
    return a

def split_states(x, n):
    x_shape = shape_list(x)
    m = x_shape[-1]
    new_x_shape = x_shape[:-1]+[n, m//n]
    return tf.reshape(x, new_x_shape)

def merge_states(x):
    x_shape = shape_list(x)
    new_x_shape = x_shape[:-2]+[np.prod(x_shape[-2:])]
    return tf.reshape(x, new_x_shape)

def split_heads(x, n, k=False):
    if k:
        return tf.transpose(split_states(x, n), [0, 2, 3, 1])
    else:
        return tf.transpose(split_states(x, n), [0, 2, 1, 3])

def merge_heads(x):
    return merge_states(tf.transpose(x, [0, 2, 1, 3]))

def conv1d(x, scope, nf, rf, w_init=tf.random_normal_initializer(stddev=0.02), b_init=tf.constant_initializer(0), pad='VALID', train=False):
    with tf.variable_scope(scope):
        nx = shape_list(x)[-1]
        w = tf.get_variable("w", [rf, nx, nf], initializer=w_init)
        b = tf.get_variable("b", [nf], initializer=b_init)
        if rf == 1: #faster 1x1 conv
            c = tf.reshape(tf.matmul(tf.reshape(x, [-1, nx]), tf.reshape(w, [-1, nf]))+b, shape_list(x)[:-1]+[nf])
        else: #was used to train LM
            c = tf.nn.conv1d(x, w, stride=1, padding=pad)+b
        return c

def attn(x, scope, n_state, n_head, train=False, scale=False):
    assert n_state%n_head==0
    with tf.variable_scope(scope):
        c = conv1d(x, 'c_attn', n_state*3, 1, train=train)
        q, k, v = tf.split(c, 3, 2)
        q = split_heads(q, n_head)
        k = split_heads(k, n_head, k=True)
        v = split_heads(v, n_head)
        a = _attn(q, k, v, train=train, scale=scale)
        a = merge_heads(a)
        a = conv1d(a, 'c_proj', n_state, 1, train=train)
        a = dropout(a, resid_pdrop, train)
        return a

def mlp(x, scope, n_state, train=False):
    with tf.variable_scope(scope):
        nx = shape_list(x)[-1]
        act = act_fns[afn]
        h = act(conv1d(x, 'c_fc', n_state, 1, train=train))
        h2 = conv1d(h, 'c_proj', nx, 1, train=train)
        h2 = dropout(h2, resid_pdrop, train)
        return h2

def block(x, scope, train=False, scale=False):
    with tf.variable_scope(scope):
        nx = shape_list(x)[-1]
        a = attn(x, 'attn', nx, n_head, train=train, scale=scale)
        n = norm(x+a, 'ln_1')
        m = mlp(n, 'mlp', nx*4, train=train)
        h = norm(n+m, 'ln_2')
        return h

def embed(X, we):
    we = convert_gradient_to_tensor(we)
    e = tf.gather(we, X)
    h = tf.reduce_sum(e, 2)
    return h

def clf(x, ny, w_init=tf.random_normal_initializer(stddev=0.02), b_init=tf.constant_initializer(0), train=False):
    with tf.variable_scope('clf'):
        nx = shape_list(x)[-1]
        w = tf.get_variable("w", [nx, ny], initializer=w_init)
        b = tf.get_variable("b", [ny], initializer=b_init)
        return tf.matmul(x, w)+b

def model(X, M, Y, train=False, reuse=False):
    with tf.variable_scope('model', reuse=reuse):
        we = tf.get_variable("we", [n_vocab+n_special+n_ctx, n_embd], initializer=tf.random_normal_initializer(stddev=0.02))
        we = dropout(we, embd_pdrop, train)

        X = tf.reshape(X, [-1, n_ctx, 2])
        M = tf.reshape(M, [-1, n_ctx])

        h = embed(X, we)
        for layer in range(n_layer):
            h = block(h, 'h%d'%layer, train=train, scale=True)

        lm_h = tf.reshape(h[:, :-1], [-1, n_embd])
        lm_logits = tf.matmul(lm_h, we, transpose_b=True)
        lm_losses = tf.nn.sparse_softmax_cross_entropy_with_logits(logits=lm_logits, labels=tf.reshape(X[:, 1:, 0], [-1]))
        lm_losses = tf.reshape(lm_losses, [shape_list(X)[0], shape_list(X)[1]-1])
        lm_losses = tf.reduce_sum(lm_losses*M[:, 1:], 1)/tf.reduce_sum(M[:, 1:], 1)

        clf_h = tf.reshape(h, [-1, n_embd])
        pool_idx = tf.cast(tf.argmax(tf.cast(tf.equal(X[:, :, 0], clf_token), tf.float32), 1), tf.int32)
        clf_h = tf.gather(clf_h, tf.range(shape_list(X)[0], dtype=tf.int32)*n_ctx+pool_idx)

        clf_h = tf.reshape(clf_h, [-1, 4, n_embd])
        if train and clf_pdrop > 0:
            shape = shape_list(clf_h)
            shape[1] = 1
            clf_h = tf.nn.dropout(clf_h, 1-clf_pdrop, shape)
        clf_h = tf.reshape(clf_h, [-1, n_embd])
        clf_logits = clf(clf_h, 1, train=train)
        clf_logits = tf.reshape(clf_logits, [-1, 4])

        clf_losses = tf.nn.sparse_softmax_cross_entropy_with_logits(logits=clf_logits, labels=Y)
        return clf_logits, clf_losses, lm_losses

def mgpu_predict(*xs):
    gpu_ops = []
    xs = (tf.split(x, n_gpu, 0) for x in xs)
    for i, xs in enumerate(zip(*xs)):
        with tf.device(assign_to_gpu(i, "/gpu:0")), tf.variable_scope(tf.get_variable_scope()):
            clf_logits, clf_losses, lm_losses = model(*xs, train=False)
            gpu_ops.append([clf_logits, clf_losses, lm_losses])
    ops = [tf.concat(op, 0) for op in zip(*gpu_ops)]
    return ops

def alignment(X1, X2):
    X1 = deepcopy(X1)
    X2 = deepcopy(X2)
    while len(X1) + len(X2) > n_ctx - 3:
        if len(X1) > len(X2):
            X1.pop(0)
        else:
            X2.pop()
    return X1, X2

def transform_race(X1, X2, X3, X4, X5):
    n_batch = len(X1)
    xmb = np.zeros((n_batch, 4, n_ctx, 2), dtype=np.int32)
    mmb = np.zeros((n_batch, 4, n_ctx), dtype=np.float32)
    start = encoder['_start_']
    delimiter = encoder['_delimiter_']
    for i, (x1, x2, x3, x4, x5), in enumerate(zip(X1, X2, X3, X4, X5)):
        tmpx1, tmpx2 = alignment(x1, x2)
        x12 = [start]+tmpx1[-max_len:]+[delimiter]+tmpx2[:max_len]+[clf_token]
        tmpx1, tmpx3 = alignment(x1, x3)
        x13 = [start]+tmpx1[-max_len:]+[delimiter]+tmpx3[:max_len]+[clf_token]
        tmpx1, tmpx4 = alignment(x1, x4)
        x14 = [start]+tmpx1[-max_len:]+[delimiter]+tmpx4[:max_len]+[clf_token]
        tmpx1, tmpx5 = alignment(x1, x5)
        x15 = [start]+tmpx1[-max_len:]+[delimiter]+tmpx5[:max_len]+[clf_token]
        l12 = len(x12)
        l13 = len(x13)
        l14 = len(x14)
        l15 = len(x15)
        xmb[i, 0, :l12, 0] = x12
        xmb[i, 1, :l13, 0] = x13
        xmb[i, 2, :l14, 0] = x14
        xmb[i, 3, :l15, 0] = x15
        mmb[i, 0, :l12] = 1
        mmb[i, 1, :l13] = 1
        mmb[i, 2, :l14] = 1
        mmb[i, 3, :l15] = 1
    xmb[:, :, :, 1] = np.arange(n_vocab+n_special, n_vocab+n_special+n_ctx)
    return xmb, mmb

def iter_apply(Xs, Ms, Ys):
    fns = [lambda x:np.concatenate(x, 0), lambda x:float(np.sum(x))]
    results = []
    for xmb, mmb, ymb in iter_data(n_batch_train, False, True, float("inf"), *(Xs, Ms, Ys)):
        n = len(xmb)
        if n == n_batch_train:
            res = sess.run([eval_mgpu_logits, eval_mgpu_clf_loss], {X_train:xmb, M_train:mmb, Y_train:ymb})
        else:
            res = sess.run([eval_logits, eval_clf_loss], {X:xmb, M:mmb, Y:ymb})
        res = [r*n for r in res]
        results.append(res)
    results = zip(*results)
    return [fn(res) for res, fn in zip(results, fns)]

def iter_predict(Xs, Ms):
    logits = []
    for xmb, mmb in iter_data(n_batch_train, False, True, float("inf"), *(Xs, Ms)):
        n = len(xmb)
        if n == n_batch_train:
            logits.append(sess.run(eval_mgpu_logits, {X_train:xmb, M_train:mmb}))
        else:
            logits.append(sess.run(eval_logits, {X:xmb, M:mmb}))
    logits = np.concatenate(logits, 0)
    return logits

def save(path):
    ps = sess.run(params)
    joblib.dump(ps, make_path(path))


argmax = lambda x:np.argmax(x, 1)

pred_fns = {
    'race':argmax,
}

filenames = {
    'race':'RACE.tsv',
}

label_decoders = {
    'race':None,
}

def predict(teX, teM):
    # filename = filenames[dataset]
    pred_fn = pred_fns[dataset]
    label_decoder = label_decoders[dataset]
    predictions = pred_fn(iter_predict(teX, teM))
    if label_decoder is not None:
        predictions = [label_decoder[prediction] for prediction in predictions]
    # path = os.path.join(submission_dir, filename)
    # os.makedirs(os.path.dirname(path))
    # with open(path, 'w') as f:
    #     f.write('{}\t{}\n'.format('index', 'prediction'))
    #     for i, prediction in enumerate(predictions):
    #         f.write('{}\t{}\n'.format(i, prediction))
    return predictions

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--desc', type=str)
    parser.add_argument('--dataset', type=str)
    parser.add_argument('--log_dir', type=str, default='log/')
    parser.add_argument('--save_dir', type=str, default='save/')
    parser.add_argument('--data_dir', type=str, default='../data/RACE')
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--n_iter', type=int, default=3)
    parser.add_argument('--n_batch', type=int, default=1)
    parser.add_argument('--max_grad_norm', type=int, default=1)
    parser.add_argument('--lr', type=float, default=6.25e-5)
    parser.add_argument('--lr_warmup', type=float, default=0.002)
    parser.add_argument('--n_ctx', type=int, default=320)
    parser.add_argument('--n_embd', type=int, default=768)
    parser.add_argument('--n_head', type=int, default=12)
    parser.add_argument('--n_layer', type=int, default=12)
    parser.add_argument('--embd_pdrop', type=float, default=0.1)
    parser.add_argument('--attn_pdrop', type=float, default=0.1)
    parser.add_argument('--resid_pdrop', type=float, default=0.1)
    parser.add_argument('--clf_pdrop', type=float, default=0.1)
    parser.add_argument('--l2', type=float, default=0.01)
    parser.add_argument('--vector_l2', action='store_true')
    parser.add_argument('--n_gpu', type=int, default=1)
    parser.add_argument('--opt', type=str, default='adam')
    parser.add_argument('--afn', type=str, default='gelu')
    parser.add_argument('--lr_schedule', type=str, default='warmup_linear')
    parser.add_argument('--encoder_path', type=str, default='model/encoder_bpe_40000.json')
    parser.add_argument('--bpe_path', type=str, default='model/vocab_40000.bpe')
    parser.add_argument('--n_transfer', type=int, default=12)
    parser.add_argument('--lm_coef', type=float, default=0.5)
    parser.add_argument('--b1', type=float, default=0.9)
    parser.add_argument('--b2', type=float, default=0.999)
    parser.add_argument('--e', type=float, default=1e-8)

    args = parser.parse_args()
    print(args)
    globals().update(args.__dict__)
    random.seed(seed)
    np.random.seed(seed)
    tf.set_random_seed(seed)

    logger = ResultLogger(path=os.path.join(log_dir, '{}.jsonl'.format(desc)), **args.__dict__)
    text_encoder = TextEncoder(encoder_path, bpe_path)
    encoder = text_encoder.encoder
    n_vocab = len(text_encoder.encoder)

    # data preprocess
    (vaX1, vaX2, vaX3, vaX4, vaX5, vaY), \
    (teX1, teX2, teX3, teX4, teX5, teY), \
    (m_teX1, m_teX2, m_teX3, m_teX4, m_teX5, m_teY), \
    (h_teX1, h_teX2, h_teX3, h_teX4, h_teX5, h_teY) = encode_dataset(text_encoder, race(data_dir, is_train=False))
    n_y = 4
    encoder['_start_'] = len(encoder)
    encoder['_delimiter_'] = len(encoder)
    encoder['_classify_'] = len(encoder)
    clf_token = encoder['_classify_']
    n_special = 3
    # max_len = n_ctx//2-2
    # n_ctx = min(max([len(x1[:max_len])+max(len(x2[:max_len]), len(x3[:max_len])) for x1, x2, x3 in zip(trX1, trX2, trX3)]+[len(x1[:max_len])+max(len(x2[:max_len]), len(x3[:max_len])) for x1, x2, x3 in zip(vaX1, vaX2, vaX3)]+[len(x1[:max_len])+max(len(x2[:max_len]), len(x3[:max_len])) for x1, x2, x3 in zip(teX1, teX2, teX3)])+3, n_ctx)
    max_len = n_ctx
    vaX, vaM = transform_race(vaX1, vaX2, vaX3, vaX4, vaX5)
    teX, teM = transform_race(teX1, teX2, teX3, teX4, teX5)
    m_teX, m_teM = transform_race(m_teX1, m_teX2, m_teX3, m_teX4, m_teX5)
    h_teX, h_teM = transform_race(h_teX1, h_teX2, h_teX3, h_teX4, h_teX5)

    n_valid = len(vaY)
    n_batch_train = n_batch*n_gpu

    # build model for training
    X_train = tf.placeholder(tf.int32, [n_batch_train, 4, n_ctx, 2])
    M_train = tf.placeholder(tf.float32, [n_batch_train, 4, n_ctx])
    X = tf.placeholder(tf.int32, [None, 4, n_ctx, 2])
    M = tf.placeholder(tf.float32, [None, 4, n_ctx])

    Y_train = tf.placeholder(tf.int32, [n_batch_train])
    Y = tf.placeholder(tf.int32, [None])


    if n_transfer == -1:
        n_transfer = 0
    else:
        n_transfer = 1+n_transfer*12

    # build model for prediction
    eval_mgpu_logits, eval_mgpu_clf_losses, eval_mgpu_lm_losses = mgpu_predict(X_train, M_train, Y_train)
    eval_logits, eval_clf_losses, eval_lm_losses = model(X, M, Y, train=False, reuse=True)
    eval_clf_loss = tf.reduce_mean(eval_clf_losses)
    eval_mgpu_clf_loss = tf.reduce_mean(eval_mgpu_clf_losses)

    # initialize trained params
    params = find_trainable_variables('model')
    sess = tf.Session(config=tf.ConfigProto(allow_soft_placement=True))
    sess.run(tf.global_variables_initializer())


    # predict
    sess.run([p.assign(ip) for p, ip in zip(params, joblib.load(os.path.join(save_dir, desc, 'best_params.jl')))])
    tePred = predict(teX, teM)
    test_accuracy = accuracy_score(teY, tePred) * 100.
    m_tePred = predict(m_teX, m_teM)
    mid_test_accuracy = accuracy_score(m_teY, m_tePred) * 100.
    h_tePred = predict(h_teX, h_teM)
    high_test_accuracy = accuracy_score(h_teY, h_tePred) * 100.
    vaPred = predict(vaX, vaM)
    val_accuracy = accuracy_score(vaY, vaPred) * 100.
    print(
'Val accuracy:{}, Test accuracy:{}, \
Test accuracy of middle: {}, \
Test accuracy of high: {}'.format(val_accuracy, test_accuracy, mid_test_accuracy, high_test_accuracy))


