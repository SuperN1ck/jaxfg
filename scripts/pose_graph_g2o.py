"""Example for solving pose graph optimization problems loaded from `.g2o` files.

For a summary of options:

    python pose_graph_g2o.py --help

"""
import argparse
import dataclasses
import enum
import pathlib

import _g2o_utils
import datargs
import matplotlib.pyplot as plt

import jaxfg


class SolverType(enum.Enum):
    GAUSS_NEWTON = jaxfg.solvers.GaussNewtonSolver()
    FIXED_ITERATION_GAUSS_NEWTON = jaxfg.solvers.FixedIterationGaussNewtonSolver(
        unroll=False
    )
    LEVENBERG_MARQUARDT = jaxfg.solvers.LevenbergMarquardtSolver()
    DOGLEG = jaxfg.solvers.DoglegSolver()

    @property
    def value(self) -> jaxfg.solvers.NonlinearSolverBase:
        """Typed override for `enum.value`."""
        value = super().value
        assert isinstance(value, jaxfg.solvers.NonlinearSolverBase)
        return value


@datargs.argsclass(
    parser_params={"formatter_class": argparse.ArgumentDefaultsHelpFormatter}
)
@dataclasses.dataclass
class CliArgs:
    g2o_path: pathlib.Path = datargs.arg(
        positional=True,
        nargs="?",
        default=pathlib.Path(__file__).parent / "data/input_M3500_g2o.g2o",
        help="Path to g2o file.",
    )
    solver_type: SolverType = datargs.arg(
        default=SolverType.GAUSS_NEWTON,
        help="Nonlinear solver to use.",
    )


def main():
    # Parse CLI args
    cli_args = datargs.parse(CliArgs)

    # Read graph
    with jaxfg.utils.stopwatch("Reading g2o file"):
        g2o: _g2o_utils.G2OData = _g2o_utils.parse_g2o(cli_args.g2o_path)

    # Make factor graph
    with jaxfg.utils.stopwatch("Making factor graph"):
        graph = jaxfg.core.StackedFactorGraph.make(g2o.factors)

    with jaxfg.utils.stopwatch("Making initial poses"):
        initial_poses = jaxfg.core.VariableAssignments.make_from_dict(g2o.initial_poses)

    # Time solver
    if not isinstance(
        cli_args.solver_type.value, jaxfg.solvers.FixedIterationGaussNewtonSolver
    ):
        # `max_iterations` field exists for all solvers but the fixed iteration GN
        with jaxfg.utils.stopwatch("Single-step JIT compile + solve"):
            solution_poses = graph.solve(
                initial_poses,
                solver=dataclasses.replace(
                    cli_args.solver_type.value, max_iterations=1
                ),
            )
            solution_poses.storage.block_until_ready()

        with jaxfg.utils.stopwatch("Single-step solve (already compiled)"):
            solution_poses = graph.solve(
                initial_poses,
                solver=dataclasses.replace(
                    cli_args.solver_type.value, max_iterations=1
                ),
            )
            solution_poses.storage.block_until_ready()

    with jaxfg.utils.stopwatch("Full solve"):
        solution_poses = graph.solve(initial_poses, solver=cli_args.solver_type.value)
        solution_poses.storage.block_until_ready()

    # Plot
    plt.figure()

    # Visualize 2D poses
    if isinstance(
        next(iter(solution_poses.get_variables())), jaxfg.geometry.SE2Variable
    ):
        plt.plot(
            *(
                initial_poses.get_stacked_value(jaxfg.geometry.SE2Variable)
                .translation()
                .T
            ),
            # Equivalent:
            # *(onp.array([initial_poses.get_value(v).translation() for v in pose_variables]).T),
            c="r",
            label="Initial",
        )
        plt.plot(
            *(
                solution_poses.get_stacked_value(jaxfg.geometry.SE2Variable)
                .translation()
                .T
            ),
            # Equivalent:
            # *(onp.array([solution_poses.get_value(v).translation() for v in pose_variables]).T),
            c="b",
            label="Optimized",
        )

    # Visualize 3D poses
    elif isinstance(
        next(iter(solution_poses.get_variables())), jaxfg.geometry.SE3Variable
    ):
        ax = plt.axes(projection="3d")
        ax.set_box_aspect((1, 1, 1))
        ax.plot3D(
            *(
                initial_poses.get_stacked_value(jaxfg.geometry.SE3Variable)
                .translation()
                .T
            ),
            c="r",
            label="Initial",
        )
        ax.plot3D(
            *(
                solution_poses.get_stacked_value(jaxfg.geometry.SE3Variable)
                .translation()
                .T
            ),
            c="b",
            label="Optimized",
        )

    else:
        assert False

    plt.title(f"Optimization on {cli_args.g2o_path.stem}")
    plt.legend()
    plt.show()


if __name__ == "__main__":
    main()
