# Copyright 2015 Novo Nordisk Foundation Center for Biosustainability, DTU.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

# http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import print_function

import warnings

from cameo.visualization import ProgressBar

import cameo
from cameo import ui
from cameo.exceptions import SolveError
from cameo.flux_analysis import flux_balance_impact_degree
from cameo.util import TimeMachine
from cameo import config
from cameo.core.solver_based_model_dual import convert_to_dual
from cameo.strain_design.strain_design import StrainDesignMethod, StrainDesign, StrainDesignResult
from sympy import Add
from cameo import flux_variability_analysis
import pandas as pd
import logging
from functools import partial

logger = logging.getLogger(__name__)


class OptKnock(StrainDesignMethod):
    """OptKnock.

    OptKnock solves a bi-level optimization problem, finding the set of knockouts that allows maximal
    target production under optimal growth.

    Parameters
    ----------
    model : SolverBasedModel
        A model to be used for finding optimal knockouts. Always set a non-zero lower bound on
        biomass reaction before using OptKnock.
    exclude_reactions : iterable of str or Reaction objects
        Reactions that will not be knocked out. Excluding reactions can give more realistic results
        and decrease running time. Essential reactions and exchanges are always excluded.
    remove_blocked : boolean (default True)
        If True, reactions that cannot carry flux (determined by FVA) will be removed from the model.
        This reduces running time significantly.

    Examples
    --------
    >>> from cameo import models
    >>> from cameo.strain_design.deterministic import OptKnock
    >>> model = models.bigg.e_coli_core
    >>> model.reactions.Biomass_Ecoli_core_w_GAM.lower_bound = 0.1
    >>> model.solver = "cplex" # Using cplex is recommended
    >>> optknock = OptKnock(model)
    >>> result = optknock.run(k=2, target="EX_ac_e", max_results=3)
    """
    def __init__(self, model, exclude_reactions=None, remove_blocked=True, *args, **kwargs):
        super(OptKnock, self).__init__(*args, **kwargs)
        self._model = model.copy()
        self._original_model = model

        if "cplex" in config.solvers:
            logger.debug("Changing solver to cplex and tweaking some parameters.")
            self._model.solver = "cplex"
            problem = self._model.solver.problem
            problem.parameters.mip.strategy.startalgorithm.set(1)
            problem.parameters.simplex.tolerances.feasibility.set(1e-8)
            problem.parameters.simplex.tolerances.optimality.set(1e-8)
            problem.parameters.mip.tolerances.integrality.set(1e-8)
            problem.parameters.mip.tolerances.absmipgap.set(1e-8)
            problem.parameters.mip.tolerances.mipgap.set(1e-8)
        else:
            warnings.warn("You are trying to run OptKnock with %s. This might not end well." %
                          self._model.solver.interface.__name__.split(".")[-1])

        if remove_blocked:
            self._remove_blocked_reactions()

        self._build_problem(exclude_reactions)

    def _remove_blocked_reactions(self):
        fva_res = flux_variability_analysis(self._model, fraction_of_optimum=0)
        blocked = [
            self._model.reactions.get_by_id(reaction) for reaction, row in fva_res.data_frame.iterrows()
            if (round(row["lower_bound"], config.ndecimals) ==
                round(row["upper_bound"], config.ndecimals) == 0)
        ]
        self._model.remove_reactions(blocked)

    def _build_problem(self, essential_reactions):
        logger.debug("Starting to formulate OptKnock problem")

        self.essential_reactions = self._model.essential_reactions() + self._model.exchanges
        if essential_reactions:
            for ess_reac in essential_reactions:
                if isinstance(ess_reac, cameo.Reaction):
                    essential_reactions.append(self._model.reactions.get_by_id(ess_reac.id))
                elif isinstance(essential_reactions, str):
                    essential_reactions.append(self._model.reactions.get_by_id(ess_reac))
                else:
                    raise TypeError(
                        "Excluded reactions must be an iterable of reactions or strings. Got object of type " +
                        str(type(ess_reac))
                    )
            self.essential_reactions += essential_reactions

        self._make_dual()

        self._combine_primal_and_dual()
        logger.debug("Primal and dual successfully combined")

        y_vars = {}
        constrained_dual_vars = set()
        for reaction in self._model.reactions:
            if reaction not in self.essential_reactions and reaction.lower_bound <= 0 <= reaction.upper_bound:
                y_var, constrained_vars = self._add_knockout_constraints(reaction)
                y_vars[y_var] = reaction
                constrained_dual_vars.update(constrained_vars)
        self._y_vars = y_vars

        primal_objective = self._model.solver.objective
        dual_objective = self._model.solver.interface.Objective.clone(
            self._dual_problem.objective, model=self._model.solver)

        reduced_expression = Add(*((c * v) for v, c in dual_objective.expression.as_coefficients_dict().items()
                                   if v not in constrained_dual_vars))
        dual_objective = self._model.solver.interface.Objective(reduced_expression, direction=dual_objective.direction)

        optimality_constraint = self._model.solver.interface.Constraint(
            primal_objective.expression - dual_objective.expression,
            lb=0, ub=0, name="inner_optimality")
        self._model.solver.add(optimality_constraint)
        logger.debug("Inner optimality constrained")

        logger.debug("Adding constraint for number of knockouts")
        knockout_number_constraint = self._model.solver.interface.Constraint(
            Add(*y_vars), lb=len(y_vars), ub=len(y_vars)
        )
        self._model.solver.add(knockout_number_constraint)
        self._number_of_knockouts_constraint = knockout_number_constraint

    def _make_dual(self):
        dual_problem = convert_to_dual(self._model.solver)
        self._dual_problem = dual_problem
        logger.debug("Dual problem successfully created")

    def _combine_primal_and_dual(self):
        primal_problem = self._model.solver
        dual_problem = self._dual_problem

        for var in dual_problem.variables:
            var = primal_problem.interface.Variable.clone(var)
            primal_problem.add(var)
        for const in dual_problem.constraints:
            const = primal_problem.interface.Constraint.clone(const, model=primal_problem)
            primal_problem.add(const)

    def _add_knockout_constraints(self, reaction):
        interface = self._model.solver.interface
        y_var = interface.Variable("y_"+reaction.id, type="binary")

        self._model.solver.add(interface.Constraint(reaction.flux_expression-1000*y_var, ub=0))
        self._model.solver.add(interface.Constraint(reaction.flux_expression+1000*y_var, lb=0))

        constrained_vars = []

        if reaction.upper_bound != 0:
            dual_forward_ub = self._model.solver.variables["dual_"+reaction.forward_variable.name+"_ub"]
            self._model.solver.add(interface.Constraint(dual_forward_ub-1000*(1-y_var), ub=0))
            constrained_vars.append(dual_forward_ub)
        if reaction.lower_bound != 0:
            dual_reverse_ub = self._model.solver.variables["dual_"+reaction.reverse_variable.name+"_ub"]
            self._model.solver.add(interface.Constraint(dual_reverse_ub - 1000*(1-y_var), ub=0))
            constrained_vars.append(dual_reverse_ub)

        return y_var, constrained_vars

    def run(self, k, target, max_results=1, *args, **kwargs):
        """
        Perform the OptKnock simulation
        :param k: The maximal allowed number of knockouts
        :param target: The reaction to be optimized
        :param max_results: The number of distinct solutions desired.
        :return: OptKnockResult
        """
        knockout_list = []
        fluxes_list = []
        production_list = []
        loader_id = ui.loading()
        with TimeMachine() as tm:
            self._model.objective = target
            self._number_of_knockouts_constraint.lb = self._number_of_knockouts_constraint.ub - k
            count = 0
            while count < max_results:
                try:
                    solution = self._model.solve()
                except SolveError as e:
                    logger.debug("Problem could not be solved. Terminating and returning "+str(count)+" solutions")
                    logger.debug(str(e))
                    break

                knockouts = set(reac for y, reac in self._y_vars.items() if round(y.primal, 3) == 0)
                assert len(knockouts) <= k

                knockout_list.append(knockouts)
                fluxes_list.append(solution.fluxes)
                production_list.append(solution.f)

                # Add an integer cut
                y_vars_to_cut = [y for y in self._y_vars if round(y.primal, 3) == 0]
                integer_cut = self._model.solver.interface.Constraint(Add(*y_vars_to_cut),
                                                                      lb=1,
                                                                      name="integer_cut_"+str(count))

                if len(knockouts) < k:
                    self._number_of_knockouts_constraint.lb = self._number_of_knockouts_constraint.ub - len(knockouts)

                tm(do=partial(self._model.solver.add, integer_cut),
                   undo=partial(self._model.solver.remove, integer_cut))
                count += 1

            ui.stop_loader(loader_id)
            return OptKnockResult(self._original_model, knockout_list, fluxes_list, production_list, target)


class RobustKnock(StrainDesignMethod):
    pass


class OptKnockResult(StrainDesignResult):
    def __init__(self, model, knockouts, fluxes, production, target, *args, **kwargs):
        super(OptKnockResult, self).__init__(*args, **kwargs)
        self._model = model
        self._knockouts = knockouts
        self._fluxes = fluxes
        self._production = production
        self._target = target
        self._processed_knockouts = None

    def _process_knockouts(self):
        progress = ProgressBar(size=len(self._knockouts), label="Processing solutions")

        self._processed_knockouts = pd.DataFrame(columns=["knockouts", "size", self._target,
                                                          "fva_min", "fva_max", "fbid"])
        progress.start()
        try:
            for i, knockouts in enumerate(self._knockouts):
                fva = flux_variability_analysis(self._model, fraction_of_optimum=0.99, reactions=[self.target])
                fbid = flux_balance_impact_degree(self._model, knockouts)
                self._processed_knockouts.loc[i] = [knockouts, len(knockouts), self.production[i],
                                                    fva[self.target]["lower_bound"], fva[self.target]["upper_bound"],
                                                    fbid.degree]
        finally:
            progress.end()

    @property
    def knockouts(self):
        return self._knockouts

    @property
    def fluxes(self):
        return self._fluxes

    @property
    def production(self):
        return self._production

    @property
    def target(self):
        return self._target

    @property
    def data_frame(self):
        if self._processed_knockouts is None:
            self._process_knockouts()
        data_frame = pd.DataFrame(self._processed_knockouts)
        data_frame.sort_values("size", inplace=True)
        return data_frame

    def _repr_html_(self):
        html_string = """
        <h3>OptKnock:</h3>
        <ul>
            <li>Target: %s</li>
        </ul>
        %s""" % (self._target, self.data_frame._repr_html_())
        return html_string

    def __len__(self):
        return len(self.knockouts)

    def __iter__(self):
        for knockouts in self.knockouts:
            yield StrainDesign(knockouts=knockouts)