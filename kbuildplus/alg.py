import sys
import os
import re
import argparse
import subprocess as sp
import pycudd
import _pycudd
import tempfile

from pymake import parser, parserdata, data, functions
from collections import defaultdict

import pdb
trace = pdb.set_trace
import z3
import vcommon as CM

from datastructures import CondDef, Multiverse, VarEntry, BoolVar, Results

import settings
mlog = CM.getLogger(__name__, settings.logger_level)

match_unexpanded_variables = re.compile(r'.*\$\(.*\).*')
def contains_unexpanded(s):
    """
    return for strings such as $(test)
    """
    assert s is None or isinstance(s, str), s

    return s is not None and match_unexpanded_variables.match(s)

    
# Placeholders for symbolic boolean operations
def conj(a, b): return None if a is None or b is None else a & b
def zconj(a, b): return None if a is None or b is None else z3.And(a, b)
def disj(a, b): return None if a is None or b is None else a | b
def zdisj(a, b): return None if a is None or b is None else z3.Or(a, b)
def neg(a): return None if a is None else ~a
def zneg(a): return None if a is None else z3.Not(a)
    
def isBooleanConfig(name):
    # if all_config_var_names is not None and not isNonbooleanConfig(name):
    #     return name in all_config_var_names
    # return False
    return False


def isNonbooleanConfig(name):
    # if nonboolean_config_var_names is not None:
    #     return name in nonboolean_config_var_names
    # return False
    return False

class  ZSolver:
    T = z3.BoolVal(True)
    F = z3.BoolVal(False)        
    
    def __init__(self):
        self.solver = z3.Solver()

    def zcheck(self, f):
        self.solver.push()
        self.solver.add(f)
        ret = self.solver.check()
        self.solver.pop()
        return ret

class Kbuild:
    def __init__(self):
        self.mgr = pycudd.DdManager()
        self.mgr.SetDefault()

        self.zsolver = ZSolver()        

        # Boolean constants
        self.T = self.mgr.One()
        self.F = self.mgr.Zero()

        self.variables = {}
        self.bvars = {}
        self.undefined_variables = set()
        # self.token_pc = {} # token presence conds, e.g., 'fork.o': True
        # self.unit_pc = {} # compilation unit presence conds
        # self.subdir_pc = {} # subdir presence conds
        # self.composite_pc = {} # composite presence conditions
        # variable equivalence classes for optimized append
        self.var_equiv_sets = {}

    def process_stmts(self, stmts, cond, zcond):
        """Find configurations in the given list of stmts under the
        given presence cond."""
        for s in stmts:
            if isinstance(s, parserdata.ConditionBlock):
                self.process_conditionblock(s, cond, zcond)
            elif isinstance(s, parserdata.SetVariable):
                self.process_setvariable(s, cond, zcond)
            elif isinstance(s, (parserdata.Rule, parserdata.StaticPatternRule)):
                self.process_rule(s, cond, zcond)
            elif (isinstance(s, parserdata.Include)):
                self.process_include(s, cond, zcond)
            else:
                mlog.warn("cannot parse ({})".format(s))

    def get_bvars(self, name):
        # Mapping of boolean variable names to BDD variable number.
        # Automatically increments the variable number.
        
        assert isinstance(name, str), name
        try:
            return self.bvars[name]
        except KeyError:
            idx = len(self.bvars)
            bdd = self.mgr.IthVar(idx)
            zbdd = z3.Bool(name.format(idx))
            bv = BoolVar(bdd, zbdd, idx)
            self.bvars[name] = bv
            return bv
    
    def get_var_equiv(self, name):
        # get a new randomized variable name that is equivalent to the
        # given variable.  this also updates a structure that records
        # which variables are equivalent
        
        assert isinstance(name, str), name

        if name in self.var_equiv_sets:
            name_ = "{}_EQUIV{}".format(len(self.var_equiv_sets[name]), name)
            self.var_equiv_sets[name].add(name_)
            return name_
        else:
            self.var_equiv_sets[name] = set([name])
            return name
        
    def get_var_equiv_set(self, name):
        if name not in self.var_equiv_sets:
            self.var_equiv_sets[name] = set([name])
            
        return self.var_equiv_sets[name]

    def getSymbTable(self, printCond=None):

        f = lambda vs: '\n'.join("{}. {}".format(i + 1, v.__str__(printCond))
                                 for i, v in enumerate(vs))

        ss = [(name, [v for v in self.variables[name] if v.val])
              for name in self.variables]
        ss = ["var: {}:\n{}\n---------".format(name, f(vs))
              for name, vs in ss if vs]
        return '\n'.join(ss)
    
    def get_presence_conditions(self, vars, pcs, cond, zcond):
        names = set()
        for var in vars:
            if var in self.variables.keys():
                names = names.union(self.get_var_equiv_set(var))
        while len(names) > 0:
            name = names.pop()
            # print name
            # print self.variables[name]
            for value, bdd_condition, z3_condition, flavor in self.variables[name]:
                # print "  ---"
                # print "  VALUE:", value
                # print "  BDD:", self.bdd_to_str(bdd_condition)
                # print "  Z3:", z3_condition
                # print "  FLAVOR:", flavor
                tokens = value.split()
                for token in tokens:
                    and_cond = conj(cond, bdd_condition)
                    and_zcond = z3.And(zcond, z3_condition)
                    if token not in pcs.keys():
                        pcs[token] = and_zcond
                    else:
                        pcs[token] = z3.Or(pcs[token], and_zcond)
                    if token.endswith(".o"): # and unit_name not in compilation_units:
                        if (token[:-2] + "-objs") in self.variables or \
                            (token[:-2] + "-y") in self.variables:
                            # scripts/Makefile.build use the -objs and -y
                            # suffix to define composites $($(subst
                            # $(obj)/,,$(@:.o=-objs))) $($(subst
                            # $(obj)/,,$(@:.o=-y)))), $^)
                            composite_variable1 = token[:-2] + "-objs"
                            composite_variable2 = token[:-2] + "-y"
                            self.get_presence_conditions([ composite_variable1, composite_variable2 ], pcs, and_cond, and_zcond)

    def add_definitions(self, defines):
        if not defines:
            return
        
        for define in defines:
            name, value = define.split("=")
            self.add_var(name, self.T, ZSolver.T, '=', value)

    def get_defined(self, variable, expected):
        variable_name = "defined(" + variable + ")"
        bdd = self.get_bvars(variable_name).bdd
        zbdd = self.get_bvars(variable_name).zbdd
        
        if expected:
            return bdd, zbdd
        else:
            return neg(bdd), zneg(zbdd)

    def process_variableref(self, name):
        if name not in self.variables and name == 'BITS':
            # TODO get real entry from top-level makefiles
            bv32 = self.get_bvars("BITS=32")
            bv64 = self.get_bvars("BITS=64")
            return Multiverse([ CondDef(bv32.bdd, bv32.zbdd, "32"),
                                CondDef(bv64.bdd, bv64.zbdd, "64") ])
        
        # elif name == 'ARCH':
        #     # TODO user-defined
        #     return Multiverse([ (self.T, "x86") ])
        elif name == "CONFIG_WORD_SIZE":

            raise NotImplementedError
            # TODO get defaults from Kconfig files
            return Multiverse([ (self.bvars["CONFIG_WORD_SIZE=32"].bdd, "32"),
                                (self.bvars["CONFIG_WORD_SIZE=64"].bdd, "64") ])

        elif name not in self.variables and name == "MMU":
            raise NotImplementedError
        
            # TODO get globals from arch Makefiles
            is_defined = self.get_defined("CONFIG_MMU", True)
            not_defined = self.get_defined("CONFIG_MMU", False)

            return Multiverse([ (is_defined, ''),
                                (not_defined, '-nommu') ])

        elif name.startswith("CONFIG_"):
            if settings.do_boolean_configs:
                #varbdd = self.bvars[name].bdd
                v = self.get_bvars(name).bdd
                zv = self.get_bvars(name).zbdd
            
                return Multiverse([ CondDef(v, zv, 'y'),
                                    CondDef(neg(v), z3.Not(zv), None) ])
            else:
                # TODO don't use 'm' for truly boolean config vars
                equals_y = self.get_bvars(name + "=y").bdd
                zequals_y = self.get_bvars(name + "=y").zbdd

                equals_m = self.get_bvars(name + "=m").bdd
                zequals_m = self.get_bvars(name + "=m").zbdd

                defined, zdefined = self.get_defined(name, True)
                is_defined_y = conj(defined, conj(equals_y, neg(equals_m)))
                zis_defined_y = z3.And(zdefined, z3.And(zequals_y, z3.Not(zequals_m)))


                is_defined_m = conj(defined, conj(equals_m, neg(equals_y)))
                zis_defined_m = z3.And(zdefined, z3.And(zequals_m, z3.Not(zequals_y)))

                notdefined, znotdefined = self.get_defined(name, False)
                not_defined = disj(notdefined, conj(neg(is_defined_y), neg(is_defined_m)))
                znot_defined = z3.Or(znotdefined, z3.And(z3.Not(zis_defined_y), z3.Not(zis_defined_m)))   


                return Multiverse([ CondDef(is_defined_y, zis_defined_y, 'y'),
                                    CondDef(is_defined_m, zis_defined_m, 'm'),
                                    CondDef(not_defined, znot_defined, None) ])
        # elif (name.startswith("CONFIG_")) and not isNonbooleanConfig(name):
        #     return Multiverse([ (self.T, '') ])
        else:
            if name in self.undefined_variables:
                return Multiverse([v.condDef for v in self.variables[name]])
            
            elif name not in self.variables and not isNonbooleanConfig(name):
                # Leave undefined variables unexpanded
                self.undefined_variables.add(name)
                self.variables[name] = [VarEntry("$(%s)" % (name),
                                                 self.T, ZSolver.T,
                                                 VarEntry.RECURSIVE)]

                mlog.warn("Undefined variable expansion: {}".format(name))
                return Multiverse([v.condDef for v in self.variables[name]])
            
            else:
                expansions = []
                equivs = self.get_var_equiv_set(name)
                for equiv_name in equivs:
                    for v in self.variables[equiv_name]:
                        if v.val:
                            expansions = expansions + self.expand_and_flatten(v.val, v.cond, v.zcond)
                        else:
                            expansions.append(v.condDef)

                return Multiverse(expansions)

    def process_function(self, function):
        """Evaluate variable expansion or built-in function and return
        either a single string or a list of (condition, string) pairs."""
        if isinstance(function, functions.VariableRef):
            return self.process_fun_VariableRef(function)
        elif isinstance(function, functions.SubstFunction):
            return self.process_fun_SubstFunction(function)
        elif isinstance(function, functions.SubstitutionRef):
            return self.process_fun_SubstitutionRef(function)
        elif isinstance(function, functions.IfFunction):
            return self.process_fun_IfFunction(function)
        elif isinstance(function, functions.FilteroutFunction):
            return self.process_fun_FilteroutFunction(function)
        elif isinstance(function, functions.PatSubstFunction):
            return self.process_fun_PatSubstFunction(function)
        elif isinstance(function, functions.SortFunction):
            return Multiverse(self.mk_Multiverse(self.process_expansion(function._arguments[0])))
        elif isinstance(function, functions.AddPrefixFunction):
            return self.process_fun_AddPrefixFunction(function)
        else:
            mlog.warn("default on function: {}".format(function))
            return self.mk_Multiverse(function.to_source())

    def process_fun_VariableRef(self, function):
        name = self.mk_Multiverse(self.process_expansion(function.vname))
        expanded_names = []
        for name_cond, name_zcond, name_value  in name:
            expanded_name = self.process_variableref(name_value)
            assert isinstance(expanded_name, Multiverse), expanded_name

            for v in expanded_name:
                expanded_names.append(
                    CondDef(conj(name_cond, v.cond), z3.And(name_zcond, v.zcond), v.mdef)
                )
        return Multiverse(expanded_names)

    def process_fun_SubstFunction(self, function):
        from_vals = self.mk_Multiverse(self.process_expansion(function._arguments[0]))
        to_vals = self.mk_Multiverse(self.process_expansion(function._arguments[1]))
        in_vals = self.mk_Multiverse(self.process_expansion(function._arguments[2]))

        # Hoist conditionals around the function by getting all
        # combinations of arguments
        hoisted_results = []
        for (sc, szc, s), (rc, rzc, r), (dc, dzc, d) in zip(from_vals, to_vals, in_vals):
            instance_cond = conj(sc, conj(rc, dc))
            instance_zcond = z3.And(szc, rzc, dzc)
            if instance_cond != self.F:  #tvn: todo check
                if r is None: r = ""  # Fixes bug in net/l2tp/Makefile
                instance_result = None if d is None else d.replace(s, r)
                hoisted_results.append(CondDef(instance_cond, instance_zcond, instance_result))

        return Multiverse(hoisted_results)
        
    def process_fun_IfFunction(self, function):
        cond_part = self.mk_Multiverse(self.process_expansion(function._arguments[0]))
        then_part = self.mk_Multiverse(self.process_expansion(function._arguments[1]))
        then_cond = self.F
        then_zcond = ZSolver.F
        else_cond = self.F
        else_zcond = ZSolver.F
        for cond, zcond, value in cond_part:
            if value:
                then_cond = disj(then_cond, cond)
                then_zcond = z3.Or(then_zcond, zcond)
            else:
                else_cond = disj(then_cond, cond)
                else_zcond = z3.Or(then_zcond, zcond)

        expansions = []

        for cond, zcond, value in then_part:
            cond = conj(then_cond, cond)
            zcond = z3.And(then_zcond, zcond)
            if cond != self.F:
                expansions.append(CondDef(cond, zcond, value))

        if len(function._arguments) > 2:
            else_part = self.mk_Multiverse(self.process_expansion(function._arguments[2]))
            for cond, zcond, value in else_part:
                cond = conj(else_cond, cond)
                zcond = z3.And(else_zcond, zcond)

                if cond != self.F:
                    expansions.append(CondDef(cond, zcond, value))

        return Multiverse(expansions)

    def process_fun_FilteroutFunction(self, function):
        from_vals = self.mk_Multiverse(self.process_expansion(function._arguments[0]))
        in_vals = self.mk_Multiverse(self.process_expansion(function._arguments[1]))

        # Hoist conditionals around the function by getting all
        # combinations of arguments
        hoisted_args = tuple((s, d) for s in from_vals for d in in_vals)
        hoisted_results = []
        # Compute the function for each combination of arguments
        for (c1, zc1, s), (c2, zc2, d) in hoisted_args:
            instance_cond = conj(c1, c2)
            instance_zcond = z3.And(zc1, zc2)

            if instance_cond != self.F:
                if d is None:
                    instance_result = None
                else:
                    instance_result = " ".join(d_token for d_token in d.split() if d_token != s)

                cd = CondDef(instance_cond, instance_zcond, instance_result)
                hoisted_results.append(cd)

        return Multiverse(hoisted_results)

    def process_fun_PatSubstFunction(self, function):
        from_vals = self.mk_Multiverse(self.process_expansion(function._arguments[0]))
        to_vals = self.mk_Multiverse(self.process_expansion(function._arguments[1]))
        in_vals = self.mk_Multiverse(self.process_expansion(function._arguments[2]))

        # Hoist conditionals around the function by getting all
        # combinations of arguments
        hoisted_args = tuple((s, r, d)
                             for s in from_vals
                             for r in to_vals
                             for d in in_vals)
        hoisted_results = []
        # Compute the function for each combination of arguments
        for (c1, zc1, s), (c2, zc2, r), (c3, zc3, d) in hoisted_args:
            instance_cond = conj(c1, conj(c2, c3))
            instance_zcond = z3.And(zc1, zc2, zc3)
            
            if instance_cond != self.F:
                if r == None: r = ""  # Fixes bug in net/l2tp/Makefile
                pattern = "^" + s.replace(r"%", r"(.*)", 1) + "$"
                replacement = r.replace(r"%", r"\1", 1)
                if d is None:
                    instance_result = None
                else:
                    instance_result = " ".join(re.sub(pattern, replacement, d_token)
                                               for d_token in d.split())
                cd = CondDef(instance_cond, instance_zcond, instance_result)
                hoisted_results.append(cd)
                
        return Multiverse(hoisted_results)

    def process_fun_AddPrefixFunction(self, function):
        prefixes = self.mk_Multiverse(self.process_expansion(function._arguments[0]))
        token_strings = self.mk_Multiverse(self.process_expansion(function._arguments[1]))

        hoisted_results = []
        for (prefix_cond, prefix_zcond, prefix) in prefixes:
            for (tokens_cond, tokens_zcond, token_string) in token_strings:
                resulting_cond = conj(prefix_cond, tokens_cond)
                resulting_zcond = z3.And(prefix_zcond, tokens_zcond)

                if resulting_cond != self.F:
                    # append prefix to each token in the token_string
                    if token_string is None:
                        prefixed_tokens = ""                            
                    else:
                        prefixed_tokens = " ".join(prefix + token
                                                   for token in token_string.split())
                    hoisted_results.append(CondDef(resulting_cond, resulting_zcond, prefixed_tokens))

        return Multiverse(hoisted_results)
        
    def process_fun_SubstitutionRef(self, function):
        raise NotImplementedError

        from_values = self.mk_Multiverse(self.process_expansion(function.substfrom))
        to_values = self.mk_Multiverse(self.process_expansion(function.substto))
        name = self.mk_Multiverse(self.process_expansion(function.vname))

        # first expand the variable
        in_values = []
        for name_cond, name_value in name:
            expanded_name = self.process_variableref(name_value)
            for (expanded_cond, expanded_value) in expanded_name:
                in_values.append( (conj(name_cond, expanded_cond), expanded_value) )

        # then do patsubst
        hoisted_arguments = tuple((s, r, d)
                                  for s in from_values
                                  for r in to_values
                                  for d in in_values)

        hoisted_results = []
        for (c1, s), (c2, r), (c3, d) in hoisted_arguments:
            instance_condition = conj(c1, conj(c2, c3))
            if instance_condition != self.F:
                if r == None: r = ""  # Fixes bug in net/l2tp/Makefile
                if r"%" not in s:
                    # without a %, use a % to implement replacing
                    # the end of the string
                    s = r"%" + s
                    r = r"%" + r
                pattern = "^" + s.replace(r"%", r"(.*)", 1) + "$"
                replacement = r.replace(r"%", r"\1", 1)
                if d is None:
                    instance_result = None
                else:
                    instance_result = " ".join([re.sub(pattern, replacement, d_token) for d_token in d.split()])
                hoisted_results.append((instance_condition, instance_result))

        return Multiverse(hoisted_results)
        
    def mk_Multiverse(self, expansion):
        assert isinstance(expansion, (str, Multiverse)), expansion
        
        if isinstance(expansion, str):
            return Multiverse([CondDef(self.T, ZSolver.T, expansion)])
        else:
            return expansion

    def process_element(self, element, isfunc):
        if isinstance(element, str):
            return element
        elif isfunc:
            ret = self.process_function(element)
            return ret
        else:
            ret = self.process_expansion(element)
            return ret

    def hoist(self, expansion):
        """Hoists a list of expansions, strings, and Multiverses.
        return a Multiverse
        """
        #trace()
        hoisted = [(self.T, ZSolver.T, [])]
        for element in expansion:
            if isinstance(element, Multiverse):
                newlist = []
                for subcondition, zsubcondition, subverse in element:
                    for condition, zcondition, verse in hoisted:
                        newcondition = conj(condition, subcondition)
                        newzcondition = z3.And(zcondition, zsubcondition)
                        
                        newverse = list(verse)
                        if isinstance(subverse, list):
                            newverse += subverse
                        else:
                            newverse.append(subverse)
                        newlist.append((newcondition, newzcondition, newverse))
                hoisted = newlist
            else:
                for condition, zcondition, verse in hoisted:
                    verse.append(element)

        multiverse = Multiverse([CondDef(condition, zcondition, self.join_values(verse))
                                 for condition, zcondition, verse in hoisted] )

        multiverse = multiverse.dedup()
        return multiverse

    def process_expansion(self, expansion):
        """Expand variables in expansion, hoisting multiply-defined ones

        @param expansion is a pymake Expansion object

        @return either a single string or a Multiverse, i.e., list of (condition, string)
        pairs."""

        if isinstance(expansion, data.StringExpansion):
            return expansion.s
        elif isinstance(expansion, data.Expansion):
            rs = [self.process_element(element, isfunc)
                  for element, isfunc in expansion]
            mv = self.hoist(rs)
            assert isinstance(mv, Multiverse), mv
            # print("process_expansion", mv)
            return mv
        else:
            mlog.error("Unsupported BaseExpansion subtype: {}".format(expansion))
            exit(1)

    def process_conditionblock(self, block, presence_cond, presence_zcond):
        """Find a satisfying configuration for each branch of the
        conditional block."""
        # See if the block has an else branch.  Assumes no "else if".
        if len(block) == 1: has_else = False
        elif len(block) == 2: has_else = True
        else:
            mlog.warn("unsupported conditional block: {}".format(block))
            return

        # Process first branch
        cond, stmts = block[0]  # condition is a Condition object
        first_branch_cond = None
        if isinstance(cond, parserdata.IfdefCondition):  # ifdef
            # TODO only care if condition.exp.variable_references contains
            # multiply-defined macros
            expansion = self.process_expansion(cond.exp)
            if isinstance(expansion, str):
                first_branch_cond, first_branch_zcond = self.get_defined(expansion, cond.expected)
            else:
                #trace()
                cds = [cd for cd in expansion if cd.mdef is not None]
                hoisted_cond = reduce(disj, [cd.cond for cd in cds])
                hoisted_zcond = z3.Or([cd.zcond for cd in cds])

                first_branch_cond = hoisted_cond
                first_branch_zcond = hoisted_zcond

            first_branch_cond = conj(presence_cond, first_branch_cond)
            first_branch_zcond = z3.And(presence_zcond, first_branch_zcond)

            else_branch_cond = neg(first_branch_cond)
            else_branch_zcond = z3.Not(first_branch_zcond)

        elif isinstance(cond, parserdata.EqCondition):  # ifeq
            exp1 = self.mk_Multiverse(self.process_expansion(cond.exp1))
            exp2 = self.mk_Multiverse(self.process_expansion(cond.exp2))

            # Hoist multiple expansions around equality operation
            hoisted_cond = self.F
            hoisted_zcond = ZSolver.F
            
            hoisted_else = self.F
            hoisted_zelse = ZSolver.F

            for cd1 in exp1:
                v1 = cd1.mdef if cd1.mdef else ""                
                for cd2 in exp2:
                    v2 = cd2.mdef if cd2.mdef else ""
                    term_cond = conj(cd1.cond, cd2.cond)
                    term_zcond = z3.And(cd1.zcond, cd2.zcond)

                    #TODO: check term_zcond == term_cond
                    
                    if term_cond != self.F and (v1 == v2) == cond.expected:
                        hoisted_cond = disj(hoisted_cond, term_cond)
                        hoisted_zcond = z3.Or(hoisted_zcond, term_zcond)
                        
                    elif term_cond != self.F:
                        hoisted_else = disj(hoisted_else, term_cond)
                        hoisted_zelse = z3.Or(hoisted_zelse, term_zcond)
                        
                    if contains_unexpanded(v1) or contains_unexpanded(v2):
                        # this preserves configurations where we
                        # didn't have values for a config option
                        v = self.get_bvars("{}={}".format(v1, v2))
                        bdd = v.bdd
                        zbdd = v.zbdd
                        hoisted_cond = disj(hoisted_cond, bdd)
                        hoisted_zcond = z3.Or(hoisted_zcond, zbdd)
                        
                        hoisted_else = disj(hoisted_else, neg(bdd))
                        hoisted_zelse = z3.Or(hoisted_zelse, z3.Not(zbdd))

                first_branch_cond = conj(presence_cond, hoisted_cond)
                first_branch_zcond = z3.And(presence_zcond, hoisted_zcond)      
                else_branch_cond = conj(presence_cond, hoisted_else)
                else_branch_zcond = z3.And(presence_zcond, hoisted_zelse)
                
        else:
            mlog.error("unsupported conditional branch: {}".format(cond))
            exit(1)

        assert first_branch_cond is not None, \
            "Could not get if branch cond {}".format(first_branch_cond)
        
        # Enter first branch
        # trace()
        # print("process_conditionblock", z3.simplify(first_branch_zcond))
        self.process_stmts(stmts, first_branch_cond, first_branch_zcond)

        if not has_else:
            return

        # Process the else branch
        cond, stmts = block[1]
        self.process_stmts(stmts, else_branch_cond, else_branch_zcond)  # Enter else branch

    def expand_and_flatten(self, val, cond, zcond):
        """Parse and expand a variable definition, flattening any
        recursive expansions by hoisting

        @return a Multiverse list of (cond, val) pairs"""
        # Parse the variable's definition
        d = parser.Data.fromstring(val, None)
        e, t, o = parser.parsemakesyntax(d, 0, (), parser.iterdata)
        if t != None or o != None:
            # TODO: do something if part of the string is left over
            pass

        expanded = self.process_expansion(e)
        # print("expanded", expanded)
        if isinstance(expanded, str):
            return Multiverse([CondDef(cond, zcond, expanded)])
        else: # must be a multiverse
            return expanded

    def join_values(self, value_list, delim=""):
        """Joins a list of make variable values that may be None, which
        means the variable is undefined.  When joined with defined values,
        None is the empty string.  When all values are None, the resulting
        value is also None.

        @param value_list a list of strings or Nones
        @returns A string or a None"""
        return delim.join(v for v in value_list if v)
    
    def append_values(self, *args):
        return self.join_values(args, " ")

    def bdd_to_cnf(self, condition):
        """Converts the expression to conjunctive normal form

        @returns a list of terms, represented as lists of factors"""
        stdout = os.dup(1)
        temp_fd, temp_filename = tempfile.mkstemp()
        sys.stdout.flush()
        os.dup2(temp_fd, 1)
        condition.PrintMinterm()
        sys.stdout.flush()
        os.dup2(stdout, 1)
        os.close(stdout)
        os.lseek(temp_fd, 0, os.SEEK_SET)
        with os.fdopen(temp_fd) as f:
            expression = []
            for line in filter(lambda l: len(l) > 0, (l.rstrip() for l in f.readlines())):
                minterm = list(line)
                term = []
                for name in self.bvars:
                    value = minterm[self.bvars[name].idx]
                    if value == '1':
                        term.append(name)
                    elif value == '0':
                        term.append("!" + name)
                    elif value == '-':
                        pass
                    else:
                        mlog.error("Unknown setting for variable in minterm: {}".format(minterm))
                        exit(1)
                        
                expression.append(term)
            os.remove(temp_filename)
            return expression
        
        mlog.error("Could not open temp file containing minterms: {}".format(temp_filename))
        exit(1)

    def bdd_to_str(self, condition):
        """Converts the expression to a string"""

        if condition == self.T:
            return "1"
        elif condition == self.F:
            return "0"

        expression = ""
        term_delim = ""
        for sublist in self.bdd_to_cnf(condition):
            term = ""
            factor_delim = ""
            for factor in sublist:
                term += factor_delim + factor
                factor_delim = " && "
            if len(term) > 0:
                expression += term_delim + term
                term_delim = " || "
        if len(expression) == 0:
            expression="1"
        return expression

    def add_var(self, name, presence_cond, presence_zcond, token, value):
        """Given a presence cond, the variable's name, its assignment
        operator, and the definition, update the variable's list of
        configurations to reflect the dry-runs needed to cover all its
        definition."""

        # print("add_var", name, z3.simplify(presence_zcond), token, value)

        assert value is not None, value

        update_vars = lambda name: \
                    map(lambda (old_value, old_cond, old_zcond, old_flavor): 
                            VarEntry(old_value, 
                                    conj(old_cond, neg(presence_cond)),
                                    z3.And(old_zcond, z3.Not(presence_zcond)),
                                    old_flavor), 
                        self.variables[name])

        if token == "=":
            # Recursively-expanded variable defs are expanded at use-time

            equivs = self.get_var_equiv_set(name)
            for equiv_name in equivs:
                # Update all existing definitions' presence conds
                if equiv_name in self.variables:
                    self.variables[equiv_name] = update_vars(equiv_name)
                else:
                    self.variables[equiv_name] = []

                # Add complete definition to variable (needed to find variable
                # expansions at use-time)
                self.variables[equiv_name].append(
                    VarEntry(value, presence_cond, presence_zcond, VarEntry.RECURSIVE))

        elif token == ":=":
            # Simply-expanded self.variables are expanded at define-time

            equivs = self.get_var_equiv_set(name)
            for equiv_name in equivs:
                # Update all existing definitions' presence conds
                # AFTER getting the new definition, since the new
                # definition might refer to itself as in
                # linux-3.0.0/crypto/Makefile

                if equiv_name in self.variables:
                    old_variables = update_vars(equiv_name)
                else:
                    # old_variables= [VarEntry(None, 
                    #     neg(presence_cond), 
                    #     z3.Not(presence_zcond), 
                    #     VarEntry.RECURSIVE)]
                    old_variables = []

                # Expand and flatten self.variables in the definition and add the
                # resulting definitions.
                new_definitions = self.expand_and_flatten(value, presence_cond, presence_zcond)
                new_variables = []
                for new_cond, new_zcond, new_value in new_definitions:
                    new_variables.append(VarEntry(new_value, 
                                                    new_cond,
                                                    new_zcond,
                                                    VarEntry.SIMPLE))

                self.variables[equiv_name] = old_variables + new_variables
                # TODO: check for computed variable names, compute them, and
                # collect any configurations resulting from those self.variables

        elif token == "+=":
            # optimize append for certain variables this
            # optimization creates a new variable entry for
            # append instead of computing the cartesian
            # product of appended variable definitions
            simply = self.F
            zsimply = ZSolver.F
            recursively = self.F
            zrecursively = ZSolver.F

            new_var_name = self.get_var_equiv(name)

            # find conds for recursively- and simply-expanded variables
            if name in self.variables:
                for entry in self.variables[name]:
                    _, old_cond, old_zcond, old_flavor = entry
                    # TODO: optimization, memoize these
                    if old_flavor == VarEntry.SIMPLE:
                        simply = disj(simply, old_cond)
                        zsimply = z3.Or(zsimply, old_zcond)
                    else:
                        assert old_flavor == VarEntry.RECURSIVE
                        recursively = disj(recursively, old_cond)
                        zrecursively = z3.Or(zrecursively, old_zcond)

                # print("+=", name, z3.simplify(zsimply), z3.simplify(zrecursively))

                #tvn: TODO: add check for zrecursively
                if recursively != self.F:
                    if new_var_name not in self.variables:
                        self.variables[new_var_name] = []
                    self.variables[new_var_name].append(
                        VarEntry(value,
                                presence_cond,
                                presence_zcond,
                                VarEntry.RECURSIVE))

                #tvn: TODO: add check for zsimply
                if simply != self.F:
                    new_definitions = self.expand_and_flatten(value, presence_cond,presence_zcond)
                    # print("simply", new_definitions)
                    new_variables = []
                    for new_cond, new_zcond, new_value in new_definitions:
                        and_zcond = z3.simplify(z3.And(new_zcond, presence_zcond))
                        if not z3.is_false(and_zcond):
                            new_variables.append(VarEntry(
                                new_value, conj(new_cond, presence_cond), and_zcond, VarEntry.SIMPLE))

                    self.variables[new_var_name] = new_variables
                    # print("simply_done", new_variables)


            else:
                self.variables[new_var_name] = [VarEntry(
                    value, presence_cond, presence_zcond, VarEntry.RECURSIVE)]       
                
                    
        elif token == "?=":
            # TODO: if ?= is used on an undefined variable, it's
            # initialized as a recursively-expanded variable (=)

            pass
        
        else:
            mlog.error("Unknown setvariable token: {}".format(token))

        # Trim definitions with a presence cond of FALSE                    
        for equiv in self.get_var_equiv_set(name):
            if equiv in self.variables:
                self.variables[equiv] = \
                    [v for v in self.variables[equiv] if v.cond != self.F]
                

    def process_setvariable(self, setvar, cond, zcond):
        """Find a satisfying set of configurations for variable."""

        assert isinstance(setvar, parserdata.SetVariable), setvar
        assert isinstance(cond, pycudd.DdNode), cond
        assert z3.is_expr(zcond), zcond

        # obj-y = 'fork.o'
        name = self.process_expansion(setvar.vnameexp)
        token = setvar.token
        value = setvar.value

        # def f(s, cond, zcond):
        #     if not (s.endswith("-y") or s.endswith("-m")):
        #         return

        #     vs = [v for v in value.split()
        #           if v.endswith(".o") or v.endswith("/")]

        #     for v in vs:
        #         if v not in self.token_pc:
        #             self.token_pc[v] = (self.T, ZSolver.T)

        #         # update nested_cond
        #         tc, tzc = self.token_pc[v]
        #         self.token_pc[v] = (conj(tc, cond), zconj(tzc, zcond))
                
        #         if s not in set(["obj-y", "obj-m", "lib-y", "lib-m"]):
        #             continue
                
        #         if v.endswith(".o"):
        #             if v in self.unit_pc:
        #                 uc, uzc = self.unit_pc[v]
        #                 tc, tzc = self.token_pc[v]
        #                 self.unit_pc[v] = (disj(uc, tc), zdisj(uzc, tzc))
        #             else:
        #                 self.unit_pc[v] = self.token_pc[v]
        #         elif v.endswith("/"):
        #             assert v not in self.subdir_pc, (v, self.subdir_pc)
        #             self.subdir_pc[v] = self.token_pc[v]
                

        if isinstance(name, str):
            # f(name, cond, zcond)  # remove because this method for presence conditions is obsolete
            self.add_var(name, cond, zcond, token, value)
        else:
            for local_cond, local_zcond, expanded_name in name:
                nested_cond = conj(local_cond, cond)
                nested_zcond = z3.And(local_zcond, zcond)
                # f(expanded_name, nested_cond, nested_zcond)  # remove because this method for presence conditions is obsolete
                self.add_var(expanded_name, nested_cond, nested_zcond, token, value)

    def process_rule(self, rule, cond, zcond):
        mlog.warn("just pass on rule {} {} {}".format(rule, self.bdd_to_str(cond), zcond))
        pass

    def process_include(self, s, cond, zcond):
        expanded_include = self.mk_Multiverse(self.process_expansion(s.exp))
        for include_cond, include_zcond, include_files in expanded_include:
            if include_files != None:
                for include_file in include_files.split():
                    obj = os.path.dirname(include_file)
                    if os.path.exists(include_file):
                        include_makefile = open(include_file, "rU")
                        s = include_makefile.read()
                        include_makefile.close()
                        include_stmts = parser.parsestring(s, include_makefile.name)
                        self.process_stmts(include_stmts, include_cond, include_zcond)

    def split_defs(self, var):
        """get every whitespace-delimited token in all definitions of the
        given var name, expanding any var invocations first"""

        if var not in self.variables:
            return []

        values = []
        for (value, cond, zcond, _) in self.variables[var]:
            assert value is not None, value
            # Expand any vars used in definitions

            expanded_values = self.mk_Multiverse(
                self.expand_and_flatten(value, cond, zcond))

            for expanded_cond, expanded_zcond, expanded_value in expanded_values:
                if expanded_value is None:
                    continue
                
                split_expanded_values = expanded_value.split()
                values.extend(split_expanded_values)

                # composite_unit = "-".join(var.split('-')[:-1]) + ".o"
                # if composite_unit not in self.token_pc:
                #     continue
                
                # for v in split_expanded_values:
                #     tc, tzc = self.token_pc[composite_unit]
                #     composite_cond = conj(expanded_cond, tc)
                #     composite_zcond = zconj(expanded_zcond, tzc)

                #     if not v in self.token_pc.keys():
                #         self.token_pc[v] = self.T, ZSolver.T

                #     # update nested_cond
                #     tc, tzc = self.token_pc[v]
                #     self.token_pc[v] = (conj(tc, composite_cond),
                #                         zconj(tzc, composite_zcond))

        return values
                

class Run:    

    def run(self, makefiledirs):
        assert isinstance(makefiledirs, (set, list)) \
            and makefiledirs, makefiledirs
        assert all(os.path.isfile(f) or os.path.isdir(f)
                   for f in makefiledirs), makefiledirs

        self.results = Results()

        subdirs = set(makefiledirs)
        while subdirs:
            makefile = subdirs.pop()
            mlog.info("processing makefile: {}".format(makefile))            
            subdirs_ = self.extract(makefile)
            if settings.do_recursive:
                subdirs += subdirs_

    def extract(self, path):
        makefile = self.get_makefile(path)

        path = os.path.dirname(makefile)
        makefile = open(makefile, "rU")

        s = makefile.read()
        makefile.close()

        kbuild = Kbuild()
        #kbuild.add_definitions(settings.define)
        stmts = parser.parsestring(s, makefile.name)

        kbuild.process_stmts(stmts, kbuild.T, ZSolver.T)

        subdirs = set()
        compilation_units = self.results.compilation_units
        composites = self.results.composites
        library_units = self.results.library_units
        hostprog_composites = self.results.hostprog_composites
        hostprog_units = self.results.hostprog_units
        unconfigurable_units = self.results.unconfigurable_units
        # unit_pcs = self.results.unit_pcs
        # subdir_pcs = self.results.subdir_pcs
        clean_files = self.results.clean_files

        pending_vars = set(
            ["obj-y", "obj-m",
             "core-y", "core-m", "drivers-y", "drivers-m",
             "net-y", "net-m", "libs-y", "libs-m", "head-y",
             "head-m"])
             
        self.collect_units(kbuild,
                           path,
                           pending_vars,
                           compilation_units,
                           subdirs,
                           composites)

        for v in set([ "subdir-y", "subdir-m" ]):
            for u in kbuild.split_defs(v):
                subdirs.add(os.path.join(path, u))

        self.collect_units(kbuild,
                           path,
                           set(["lib-y", "lib-m" ]),
                           library_units,
                           subdirs,
                           composites)

        pending_hostprog_composites = set()
        for v in set([ "hostprogs-y", "hostprogs-m", "host-progs", "always" ]):
            for u in kbuild.split_defs(v):
                composite_name = u + "-objs"
                unit_name = os.path.join(path, u)
                if composite_name in kbuild.variables:
                    pending_hostprog_composites.add(composite_name)
                    hostprog_composites.add(unit_name)
                else:
                    hostprog_units.add(unit_name)

        if pending_hostprog_composites:
            raise NotImplementedError
            self.collect_units(kbuild,
                               path,
                               pending_hostprog_composites,
                               hostprog_units,
                               None,
                               hostprog_composites)

        for v in set([ "targets", "extra-y" ]):
            for u in kbuild.split_defs(v):
                unit_name = os.path.join(path, u)
                if unit_name.endswith(".o"):
                    raise NotImplementedError
                    #extra_targets.add(unit_name)

        for u in kbuild.split_defs("targets"):
            if u.endswith(".c"):
                raise NotImplementedError
                #c_file_targets.add(os.path.join(obj, u))

        for u in kbuild.split_defs("clean-files"):
            clean_files.add(os.path.join(path, u))
            

        # look for variables starting with obj-, lib-, hostprogs-,
        # compositename-, or hostprog-compositename-.  doesn't account for
        # composites in the unconfigurable variables
        unconfigurable_prefixes = set([ "obj-$", "lib-$", "hostprogs-$" ])
        for cset in (composites, hostprog_composites):
            for c in cset:
                c = os.path.basename(c)
                if c.endswith(".o"):
                    c = c[:-2]
                unconfigurable_prefixes.add(c + "-$")
        unconfigurable_variables = set([])
        for x in kbuild.variables:
            for p in unconfigurable_prefixes:
                if x.startswith(p) and \
                        not x.endswith("-") and \
                        not x.endswith("-y") and \
                        not x.endswith("-m") and \
                        not x.endswith("-objs") and \
                        x != "host-progs":
                    unconfigurable_variables.add(x)
                elif x.startswith(p[:-1]) and x.endswith("-"):
                    # also look in variables resulting from expansion of
                    # undefined config var
                    unconfigurable_variables.add(x)
        self.collect_units(kbuild,
                           path,
                           unconfigurable_variables,
                           unconfigurable_units,
                           unconfigurable_units,
                           unconfigurable_units)

        # subtract out compilation units that are configurable
        unconfigurable_units.difference_update(compilation_units)
        unconfigurable_units.difference_update(library_units)
        unconfigurable_units.difference_update(composites)
        unconfigurable_units.difference_update(subdirs)

        # look for variable expansions or function calls in
        # compilation_units, subdirs, and variable names
        self.check_unexpanded_vars(compilation_units, "compilation unit")
        self.check_unexpanded_vars(subdirs, "subdirectory")
        self.check_unexpanded_vars(kbuild.variables.keys(), "variable name")

        if settings.do_table:
            mlog.info(kbuild.getSymbTable(printCond=kbuild.bdd_to_str))

        presence_conditions = {}
        kbuild.get_presence_conditions([ "obj-y", "obj-m", "lib-y", "lib-m" ], presence_conditions, kbuild.T, ZSolver.T)
        for token in presence_conditions:
            filename = os.path.join(path, token)
            # print "FILENAME", filename
            if filename not in self.results.presence_conditions.keys():
                self.results.presence_conditions[filename] = presence_conditions[token]
            else:
                self.results.presence_conditions[filename] = z3.Or(self.results.presence_conditions[filename], presence_conditions[token])

        # print self.results.presence_conditions

        # removed because this method for getting presence conditions is obsolete
        # def _f(d, s):
        #     for name, (cond, zcond) in d.iteritems():
        #         s.add((os.path.normpath(os.path.join(path, name)), kbuild.bdd_to_str(cond), zcond))

        # _f(kbuild.unit_pc, unit_pcs)
        # _f(kbuild.subdir_pc, subdir_pcs)

        #clean up  
        _pycudd.delete_DdManager(kbuild.mgr)

        return subdirs

    @classmethod
    def get_makefile(cls, path):
        #use Kbuild file if found, otherwise try Makefile
        if os.path.isdir(path):
            makefile = os.path.join(path, "Kbuild")
            if not os.path.isfile(makefile):
                makefile = os.path.join(path, "Makefile")

                if not os.path.isfile(makefile):
                    mlog.error("{} not found".format(makefile))
                    exit(1)
        else:
            assert os.path.isfile(path), path
            makefile = path                

        return makefile

    @classmethod
    def collect_units(cls,
                      kbuild,            # current kbuild instance
                      path,               # current path
                      pending_vars, # variables to extract from, gets emptied
                      compilation_units, # adds units to this set
                      subdirs,    # adds subdir to this set
                      composites):  # save pcs from these variables

        """fixed-point algorithm that adds composites and stops when no
        more variables to look at are available"""

        assert isinstance(kbuild, Kbuild), kbuild
        assert path is None or os.path.isdir(path), path
        assert isinstance(compilation_units, set), compilation_units
        assert isinstance(subdirs, set), subdirs
        assert isinstance(composites, set), composites

        equiv_sets = [kbuild.get_var_equiv_set(v) for v in pending_vars]
        equiv_vars = set.union(*equiv_sets) if equiv_sets else set()
        pending_vars |= equiv_vars

        processed_vars = set()        
        while pending_vars:
            pending_var = pending_vars.pop()
            processed_vars.add(pending_var)

            values = kbuild.split_defs(pending_var)
            for elem in values:
                cls.collect_defs(elem, kbuild, path,
                                 compilation_units,
                                 subdirs, composites, 
                                 processed_vars, pending_vars)
                
    @classmethod
    def collect_defs(cls, elem,
                     kbuild,
                     path,
                     compilation_units,
                     subdirs,
                     composites,
                     processed_vars,
                     pending_vars):

        unit_name = os.path.normpath(os.path.join(path, elem))
        if elem.endswith(".o") and unit_name not in compilation_units:
            # Expand composites
            if (elem[:-2] + "-objs") in kbuild.variables or \
                (elem[:-2] + "-y") in kbuild.variables:
                # scripts/Makefile.build use the -objs and -y
                # suffix to define composites $($(subst
                # $(obj)/,,$(@:.o=-objs))) $($(subst
                # $(obj)/,,$(@:.o=-y)))), $^)
                composite_variable1 = elem[:-2] + "-objs"
                composite_variable2 = elem[:-2] + "-y"

                if composite_variable1 not in processed_vars and \
                        composite_variable2 not in processed_vars:
                    composites.add(unit_name)
                    pending_vars.update(kbuild.get_var_equiv_set(composite_variable1))
                    pending_vars.update(kbuild.get_var_equiv_set(composite_variable2))
                    # if (elem not in kbuild.token_pc):
                    #     raise NotImplementedError
                    #     kbuild.token_pc[elem] = (kbuild.T, ZSolver.T)
                    # kbuild.composite_pc[elem] = kbuild.token_pc[elem]

                if os.path.isfile(unit_name[:-2] + ".c") or os.path.isfile(unit_name[:-2] + ".S"): 
                    compilation_units.add(unit_name)
                    # if (elem not in kbuild.token_pc): 
                    #     kbuild.token_pc[elem] = (kbuild.T, ZSolver.T)
                    # kbuild.unit_pc[elem] = kbuild.token_pc[elem]
            else:
                compilation_units.add(unit_name)
                # if (elem not in kbuild.token_pc):
                #     kbuild.token_pc[elem] = (kbuild.T, ZSolver.T)
                # kbuild.unit_pc[elem] = kbuild.token_pc[elem]

        elif elem.endswith("/"):
            # scripts/Makefile.lib takes anything that
            # ends in a forward slash as a subdir
            # $(patsubst %/,%,$(filter %/, $(obj-y)))
            new_dir = elem
            if not new_dir.startswith('/'):
                new_dir = os.path.join(path, new_dir)

            if os.path.isdir(new_dir):
                subdirs.add(new_dir)

            # if elem not in kbuild.token_pc:
            #     kbuild.token_pc[elem] = kbuild.T
            # kbuild.subdir_pc[elem] = kbuild.token_pc[elem]
    
    @classmethod
    def check_unexpanded_vars(cls, l, desc):
        for x in l:
            if contains_unexpanded(x):
                mlog.warn("A {} contains an unexpanded variable or call {}".format(desc, x))
