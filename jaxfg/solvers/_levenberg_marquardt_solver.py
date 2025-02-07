from typing import TYPE_CHECKING

import jax_dataclasses
from jax import numpy as jnp
from overrides import overrides

from .. import hints, sparse
from ..core._variable_assignments import VariableAssignments
from ._mixins import _TerminationCriteriaMixin, _TrustRegionMixin
from ._nonlinear_solver_base import NonlinearSolverBase, NonlinearSolverState

if TYPE_CHECKING:
    from ..core._stacked_factor_graph import StackedFactorGraph


@jax_dataclasses.pytree_dataclass
class _LevenbergMarquardtState(NonlinearSolverState):
    """State passed between LM iterations."""

    lambd: hints.Scalar


@jax_dataclasses.pytree_dataclass
class LevenbergMarquardtSolver(
    NonlinearSolverBase[_LevenbergMarquardtState],
    _TerminationCriteriaMixin,
    _TrustRegionMixin,
):
    """Simple damped least-squares implementation."""

    lambda_initial: hints.Scalar = 5e-4
    lambda_factor: hints.Scalar = 2.0
    lambda_min: hints.Scalar = 1e-5
    lambda_max: hints.Scalar = 1e10

    @overrides
    def _initialize_state(
        self,
        graph: "StackedFactorGraph",
        initial_assignments: VariableAssignments,
    ) -> _LevenbergMarquardtState:
        # Initialize
        cost, residual_vector = graph.compute_cost(initial_assignments)
        return _LevenbergMarquardtState(
            iterations=0,
            assignments=initial_assignments,
            cost=cost,
            residual_vector=residual_vector,
            done=False,
            lambd=self.lambda_initial,
        )

    @overrides
    def _step(
        self,
        graph: "StackedFactorGraph",
        state_prev: _LevenbergMarquardtState,
    ) -> _LevenbergMarquardtState:
        # There's currently some redundancy here: we only need to re-linearize when
        # updates are accepted

        self._hcb_print(
            lambda i, max_i, cost, lambd: f"Iteration #{i}/{max_i}: cost={str(cost).ljust(15)} lambda={str(lambd)}",
            i=state_prev.iterations,
            max_i=self.max_iterations,
            cost=state_prev.cost,
            lambd=state_prev.lambd,
        )

        # Linearize graph
        A: sparse.SparseCooMatrix = graph.compute_whitened_residual_jacobian(
            assignments=state_prev.assignments,
            residual_vector=state_prev.residual_vector,
        )
        ATb = A.T @ -state_prev.residual_vector

        # Solve linear subproblem
        step_vector: jnp.ndarray = self.linear_solver.solve_subproblem(
            A=A,
            ATb=ATb,
            lambd=state_prev.lambd,
            iteration=state_prev.iterations,
        )
        local_delta_assignments = VariableAssignments(
            storage=step_vector,
            storage_metadata=graph.local_storage_metadata,
        )

        # On-manifold retraction + solution check
        assignments_proposed = state_prev.assignments.manifold_retract(
            local_delta_assignments=local_delta_assignments
        )
        proposed_cost, residual_vector = graph.compute_cost(assignments_proposed)
        accept_flag = (
            self.compute_step_quality(
                A=A,
                proposed_cost=proposed_cost,
                state_prev=state_prev,
                step_vector=step_vector,
            )
            >= self.step_quality_min
        )

        # Update damping
        # In the future, we may consider more sophisticated lambda updates, eg:
        # > METHODS FOR NON-LINEAR LEAST SQUARES PROBLEM, Madsen et al 2004.
        # > pg. 27, Algorithm 3.16
        lambd = jnp.where(
            accept_flag,
            # If accept, decrease damping: note that we *don't* enforce any bounds here
            state_prev.lambd / self.lambda_factor,
            # If reject: increase lambda and enforce bounds
            jnp.maximum(
                self.lambda_min,
                jnp.minimum(state_prev.lambd * self.lambda_factor, self.lambda_max),
            ),
        )

        # Get output assignments
        assignments = jax_dataclasses.replace(
            state_prev.assignments,
            storage=jnp.where(
                accept_flag,
                assignments_proposed.storage,
                state_prev.assignments.storage,
            ),
        )

        # Check for convergence
        done = jnp.logical_or(
            self.check_exceeded_max_iterations(state_prev=state_prev),
            jnp.logical_and(
                accept_flag,
                self.check_convergence(
                    state_prev=state_prev,
                    cost_updated=proposed_cost,
                    local_delta_assignments=local_delta_assignments,
                    negative_gradient=ATb,
                ),
            ),
        )

        return _LevenbergMarquardtState(
            iterations=state_prev.iterations + 1,
            assignments=assignments,
            lambd=lambd,
            cost=jnp.where(
                accept_flag, proposed_cost, state_prev.cost
            ),  # Use old cost if update is rejected
            residual_vector=residual_vector,
            done=done,
        )
