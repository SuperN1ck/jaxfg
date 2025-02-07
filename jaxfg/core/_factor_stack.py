from typing import Generic, List, Sequence, Tuple, Type, TypeVar

import jax
import jax_dataclasses
import numpy as onp
from jax import numpy as jnp

from .. import hints, sparse
from ._factor_base import FactorBase
from ._variable_assignments import StorageMetadata, VariableAssignments
from ._variables import VariableBase

FactorType = TypeVar("FactorType", bound=FactorBase)


@jax_dataclasses.pytree_dataclass
class FactorStack(Generic[FactorType]):
    """A set of factors, with their parameters stacked."""

    num_factors: int = jax_dataclasses.static_field()
    factor: FactorType
    value_indices: Tuple[hints.Array, ...]

    def __post_init__(self):
        # There should be one set of indices for each variable type.
        assert len(self.value_indices) == len(self.factor.variables)

    # Shape checks break under vmap
    #     # Check that shapes make sense.
    #     for variable, indices in zip(self.factor.variables, self.value_indices):
    #         residual_dim = self.factor.noise_model.get_residual_dim()
    #         assert indices.shape == (
    #             self.num_factors,
    #             variable.get_parameter_dim(),
    #         )
    #         assert residual_dim == self.factor.get_residual_dim()

    @staticmethod
    def make(
        factors: Sequence[FactorType],
        storage_metadata: StorageMetadata,
        use_onp: bool,
    ) -> "FactorStack[FactorType]":
        """Make a stacked factor."""

        # For one-off computations, onp has much less overhead than jnp.
        jnp = onp if use_onp else globals()["jnp"]

        # Stack factors in our group.
        # This requires that the treedefs of each factor match, which won't be
        # the case when factors are connected to different variables!
        stacked_factor: FactorType = jax.tree_map(
            lambda *arrays: jnp.stack(arrays, axis=0),
            *map(FactorBase.anonymize_variables, factors),  # type: ignore
            # > https://github.com/python/mypy/issues/1317
        )

        # Get indices for each variable of each factor.
        value_indices_list: Tuple[List[onp.ndarray], ...] = tuple(
            [] for _ in range(len(stacked_factor.variables))
        )
        for factor in factors:
            for i, variable in enumerate(factor.variables):
                assert isinstance(
                    variable, type(factors[0].variables[i])
                ), "Variable types of stacked factors must match"
                storage_pos = storage_metadata.index_from_variable[variable]
                value_indices_list[i].append(
                    onp.arange(storage_pos, storage_pos + variable.get_parameter_dim())
                )

        # Stack: end result should be Tuple[array of shape (N, parameter_dim), ...].
        value_indices_stacked: Tuple[onp.ndarray, ...] = tuple(
            onp.array(indices) for indices in value_indices_list
        )

        # Record values.
        return FactorStack(
            num_factors=len(factors),
            factor=stacked_factor,
            value_indices=value_indices_stacked,
        )

    @staticmethod
    def compute_jacobian_coords(
        factors: Sequence[FactorType],
        local_storage_metadata: StorageMetadata,
        row_offset: int,
    ) -> List[sparse.SparseCooCoordinates]:
        """Computes Jacobian coordinates for a factor stack. One array of indices per
        variable."""

        variable_types: List[Type[VariableBase]] = [
            type(v) for v in factors[0].variables
        ]

        # Get indices for each variable.
        local_value_indices_list: Tuple[List[onp.ndarray], ...] = tuple(
            [] for _ in range(len(variable_types))
        )
        for factor in factors:
            for i, variable in enumerate(factor.variables):
                # Record local parameterization indices.
                storage_pos = local_storage_metadata.index_from_variable[variable]
                local_value_indices_list[i].append(
                    onp.arange(
                        storage_pos,
                        storage_pos + variable.get_local_parameter_dim(),
                    )
                )

        # Stack: end result should be Tuple[array of shape (N, parameter_dim), ...].
        local_value_indices_stacked: Tuple[onp.ndarray, ...] = tuple(
            onp.array(indices) for indices in local_value_indices_list
        )

        # Get residual indices.
        num_factors = len(factors)
        residual_dim = factors[0].get_residual_dim()
        residual_indices = onp.arange(num_factors * residual_dim).reshape(
            (num_factors, residual_dim)
        )

        # Get Jacobian coordinates.
        jacobian_coords: List[sparse.SparseCooCoordinates] = []
        for variable_index, variable_type in enumerate(variable_types):
            variable_dim = variable_type.get_local_parameter_dim()

            coords = onp.stack(
                (
                    # Row indices.
                    onp.broadcast_to(
                        residual_indices[:, :, None],
                        (num_factors, residual_dim, variable_dim),
                    )
                    + row_offset,
                    # Column indices.
                    onp.broadcast_to(
                        local_value_indices_stacked[variable_index][:, None, :],
                        (num_factors, residual_dim, variable_dim),
                    ),
                ),
                axis=-1,
            ).reshape((num_factors * residual_dim * variable_dim, 2))

            jacobian_coords.append(
                sparse.SparseCooCoordinates(
                    rows=coords[:, 0],
                    cols=coords[:, 1],
                )
            )

        return jacobian_coords

    def get_residual_dim(self) -> int:
        return self.factor.get_residual_dim() * self.num_factors

    def compute_residual_vector(self, assignments: VariableAssignments) -> jnp.ndarray:
        """Compute stacked residual vectors.

        Shape of output should be `(N, stacked_factor.factor.get_residual_dim())`.
        """

        # Stack inputs to our factors.
        values_stacked = tuple(
            jax.vmap(type(variable).unflatten)(assignments.storage[indices])
            for variable, indices in zip(self.factor.variables, self.value_indices)
        )

        # Vectorized residual computation.
        # The type of `values_stacked` should match `FactorVariableValues`.
        residual_vector = jax.vmap(type(self.factor).compute_residual_vector)(
            self.factor,
            self.factor.build_variable_value_tuple(values_stacked),
        )
        return residual_vector

    def compute_residual_jacobian(
        self,
        assignments: VariableAssignments,
    ) -> Tuple[jnp.ndarray, ...]:
        """Compute stacked Jacobian matrices, one for each variable.

        Shape of each Jacobian array should be `(N, local parameter dim, residual dim)`.
        """

        # Stack inputs to our factors.
        values_stacked = tuple(
            jax.vmap(variable.unflatten)(assignments.storage[indices])
            for indices, variable in zip(self.value_indices, self.factor.variables)
        )

        # Compute Jacobians wrt local parameterizations.
        # The type of `values_stacked` should match `FactorVariableValues`.
        jacobians = jax.vmap(type(self.factor).compute_residual_jacobians)(
            self.factor,
            self.factor.build_variable_value_tuple(values_stacked),
        )
        return jacobians
