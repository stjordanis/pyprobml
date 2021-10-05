# Author : Kevin Murphy(@murphyk), Aleyna Kara(@karalleyna)

import jax
import jax.numpy as jnp
from jax import jit, random, vmap
from jax.random import split
from jax import jit, tree_leaves, tree_map, vmap

import optax

from sgmcmc_utils import build_optax_optimizer


def generate_random_basis(key, full_dim, subspace_dim):
    projection_matrix = random.normal(key, shape=(subspace_dim, full_dim))
    projection_matrix = projection_matrix / jnp.linalg.norm(projection_matrix, axis=-1, keepdims=True)
    return projection_matrix


@jit
def convert_params_from_subspace_to_full(params_subspace, projection_matrix, params_full_init):
    return jnp.matmul(params_subspace, projection_matrix) + params_full_init


def make_subspace_fns(loglikelihood, logprior, anchor_params_tree, projection_matrix):
    anchor_params_full, flat_to_pytree_fn = jax.flatten_util.ravel_pytree(anchor_params_tree)
    subspace_dim, full_dim = projection_matrix.shape

    def subspace_to_pytree_fn(params_subspace):
        params_full = convert_params_from_subspace_to_full(params_subspace, projection_matrix, anchor_params_full)
        params_pytree = flat_to_pytree_fn(params_full)
        return params_pytree

    def loglikelihood_subspace(params_subspace, *args):
        params_pytree = subspace_to_pytree_fn(params_subspace)
        return loglikelihood(params_pytree, *args)

    #def logprior_subspace(params_subspace):
    #     # Spherical Gaussian prior
    ##     # See "Subspace inference for Bayesian deep learning", https://arxiv.org/abs/1907.07504
    #    logp = jax.vmap(lambda p: jax.scipy.stats.norm.logpdf(p, scale=l2), params_subspace)
    #    return jnp.sum(logp)

    def logprior_subspace(params_subspace):
        params_pytree = subspace_to_pytree_fn(params_subspace)
        return logprior(params_pytree)

    return loglikelihood_subspace, logprior_subspace, subspace_to_pytree_fn

def init_subspace_rnd(key, loglikelihood, logprior, params_init_tree,  subspace_dim):
    params_init_flat, _ = jax.flatten_util.ravel_pytree(params_init_tree)
    full_dim = len(params_init_flat)
    key, mykey = split(key)
    projection_matrix = generate_random_basis(mykey, full_dim, subspace_dim)

    loglik_sub, logprior_sub, subspace_to_pytree_fn = make_subspace_fns(
        loglikelihood, logprior, params_init_tree, projection_matrix)
    subspace_fns = (loglik_sub, logprior_sub, subspace_to_pytree_fn)
    return subspace_fns

def init_subspace_opt(key, loglikelihood, logprior, params_init_tree, subspace_dim,
                    data, batch_size, nwarmup, opt, use_svd=False, pbar=False):
    # find good anchor in full dimensional parameter space
    if nwarmup > 0:
        #def logprior(params):
        #    leaves_of_params = tree_leaves(params)
        #    return jnp.sum(tree_map(lambda p: jnp.sum(jax.scipy.stats.norm.logpdf(p, scale=l2)), leaves_of_params))
        optimizer = build_optax_optimizer(opt, loglikelihood, logprior, data, batch_size, pbar)
        key, mykey = split(key)
        params_init_tree, _ = optimizer(mykey, nwarmup, params_init_tree)

    # find good projection matrix
    if use_svd:
        raise('not yet implemented')
    else:
        params_init_flat, _ = jax.flatten_util.ravel_pytree(params_init_tree)
        full_dim = len(params_init_flat)
        key, mykey = split(key)
        projection_matrix = generate_random_basis(mykey, full_dim, subspace_dim)

    subspace_fns = make_subspace_fns(
        loglikelihood, logprior, params_init_tree, projection_matrix)
    return subspace_fns

def subspace_optimizer(key, subspace_fns, data, batch_size,  nsteps, opt, params_subspace, pbar=True):
    loglik_sub, logprior_sub, subspace_to_pytree_fn = subspace_fns
    optimizer_sub = build_optax_optimizer(opt, loglik_sub, logprior_sub, data, batch_size, pbar)
    key, mykey = split(key)
    params_subspace, log_post_trace = optimizer_sub(mykey, nsteps, params_subspace)
    params_tree = subspace_to_pytree_fn(params_subspace)
    return params_tree, params_subspace, log_post_trace


def subspace_sampler(key, subspace_fns, data, batch_size,  nsamples, build_sampler, params_subspace, use_cv=True, pbar=True):
    loglik_sub, logprior_sub, subspace_to_pytree_fn = subspace_fns
    if use_cv:
        sampler_sub = build_sampler(loglikelihood=loglik_sub, logprior=logprior_sub, data=data, batch_size=batch_size,
                                    centering_value=params_subspace, pbar=pbar)
    else:
        sampler_sub = build_sampler(loglikelihood=loglik_sub, logprior=logprior_sub, data=data,
                                    batch_size=batch_size, pbar=pbar)
    key, mykey = split(key)
    params_sub_samples = sampler_sub(mykey, nsamples, params_subspace)
    params_tree_samples = vmap(subspace_to_pytree_fn)(params_sub_samples)
    return params_tree_samples, params_sub_samples



def subspace_optimizer_old(key, loglikelihood, logprior, params_init_tree, data, batch_size, subspace_dim, nwarmup,
                       nsteps, opt=optax.adam(learning_rate=0.1), projection_matrix=None, pbar=True):
    opt_key, subspace_key, sub_init_key, sub_opt_key = split(key, 4)

    # Find good anchor in full space during warmup phase
    if nwarmup > 0:
        optimizer = build_optax_optimizer(opt, loglikelihood, logprior, data, batch_size, pbar)
        params_init_tree, _ = optimizer(opt_key, nwarmup, params_init_tree)

    # Make Random subspace
    if projection_matrix is None:
        params_init_flat, _ = jax.flatten_util.ravel_pytree(params_init_tree)
        full_dim = len(params_init_flat)
        projection_matrix = generate_random_basis(subspace_key, full_dim, subspace_dim)
    # TODO: add SVD

    loglik_sub, logprior_sub, subspace_to_pytree_fn = make_subspace_fns(
        loglikelihood, logprior, params_init_tree, projection_matrix)
    subspace_fns = (loglik_sub, logprior_sub, subspace_to_pytree_fn)

    # Do subspace optimization starting from rnd location
    params_subspace = jax.random.normal(sub_init_key, (subspace_dim,))
    optimizer_sub = build_optax_optimizer(opt, loglik_sub, logprior_sub, data, batch_size, pbar)

    params_subspace, log_post_trace = optimizer_sub(sub_opt_key, nsteps, params_subspace)
    params_tree = subspace_to_pytree_fn(params_subspace)

    return params_tree, params_subspace, log_post_trace, subspace_fns


def subspace_sampler_old(key, loglikelihood, logprior, params_init_tree, build_sampler, data, batch_size,
                     subspace_dim, nsamples, opt=optax.adam(learning_rate=0.1),
                     nsteps_full=0, nsteps_sub=0, projection_matrix=None, use_cv=True, pbar=True):
    subspace_key, sample_key = split(key)

    if nsteps_full > 0 or nsteps_sub > 0:
        # Find good control variate / starting point in subspace
        params_tree, params_sub, log_post_trace, subspace_fns = subspace_optimizer(
            subspace_key, loglikelihood, logprior, params_init_tree, data, batch_size,
            subspace_dim, nsteps_full, nsteps_sub, opt, pbar=pbar)
    else:
        params_sub = jax.random.normal(subspace_key, (subspace_dim,))
        params_init_flat, _ = jax.flatten_util.ravel_pytree(params_init_tree)
        full_dim = len(params_init_flat)
        if projection_matrix is None:
            projection_matrix = generate_random_basis(subspace_key, full_dim, subspace_dim)
        subspace_fns = make_subspace_fns(loglikelihood, logprior, params_init_tree, projection_matrix)

    loglik_sub, logprior_sub, subspace_to_pytree_fn = subspace_fns
    
    if use_cv:
        sampler_sub = build_sampler(loglikelihood=loglik_sub, logprior=logprior_sub, data=data, batch_size=batch_size,
                                    centering_value=params_sub, pbar=pbar)
    else:
        sampler_sub = build_sampler(loglikelihood=loglik_sub, logprior=logprior_sub, data=data,
                                    batch_size=batch_size, pbar=pbar)

    params_sub_samples = sampler_sub(sample_key, nsamples, params_sub)
    params_tree_samples = vmap(subspace_to_pytree_fn)(params_sub_samples)

    return params_tree_samples, params_sub_samples, subspace_fns
