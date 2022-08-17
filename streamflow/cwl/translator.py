from __future__ import annotations

import copy
import importlib
import os
import posixpath
import tempfile
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, MutableMapping, MutableSequence, Optional, Set, Type, Union, cast

import cwl_utils.parser
import cwl_utils.parser.cwl_v1_0_utils
import cwl_utils.parser.cwl_v1_1_utils
import cwl_utils.parser.cwl_v1_2_utils
from rdflib import Graph

from streamflow.config.config import WorkflowConfig
from streamflow.core.context import StreamFlowContext
from streamflow.core.data import LOCAL_LOCATION
from streamflow.core.deployment import DeploymentConfig, LocalTarget, Target
from streamflow.core.exception import WorkflowDefinitionException
from streamflow.core.utils import random_name
from streamflow.core.workflow import CommandOutputProcessor, Port, Step, Token, TokenProcessor, Workflow
from streamflow.cwl import utils
from streamflow.cwl.combinator import ListMergeCombinator
from streamflow.cwl.command import (
    CWLCommand, CWLCommandToken, CWLExpressionCommand, CWLMapCommandToken, CWLObjectCommandToken, CWLUnionCommandToken
)
from streamflow.cwl.hardware import CWLHardwareRequirement
from streamflow.cwl.processor import (
    CWLCommandOutputProcessor, CWLMapCommandOutputProcessor, CWLMapTokenProcessor, CWLObjectCommandOutputProcessor,
    CWLObjectTokenProcessor, CWLTokenProcessor, CWLUnionCommandOutputProcessor, CWLUnionTokenProcessor
)
from streamflow.cwl.step import (
    CWLConditionalStep, CWLEmptyScatterConditionalStep, CWLInputInjectorStep,
    CWLLoopConditionalStep, CWLLoopOutputAllStep, CWLLoopOutputLastStep, CWLTransferStep
)
from streamflow.cwl.transformer import (
    AllNonNullTransformer, CWLDefaultTransformer, CWLTokenTransformer, FirstNonNullTransformer, ForwardTransformer,
    ListToElementTransformer, LoopValueFromTransformer, OnlyNonNullTransformer, ValueFromTransformer
)
from streamflow.cwl.utils import LoadListing, SecondaryFile, resolve_dependencies
from streamflow.log_handler import logger
from streamflow.workflow.combinator import (
    CartesianProductCombinator, DotProductCombinator, LoopCombinator,
    LoopTerminationCombinator
)
from streamflow.workflow.step import (
    Combinator, CombinatorStep, DeployStep, ExecuteStep, GatherStep, LoopCombinatorStep, ScatterStep, ScheduleStep,
    Transformer
)
from streamflow.workflow.token import TerminationToken


def _convert_stdstreams_to_files(process: Union[cwl_utils.parser.cwl_v1_0.Process,
                                                cwl_utils.parser.cwl_v1_1.Process,
                                                cwl_utils.parser.cwl_v1_2.Process]):
    if isinstance(process, cwl_utils.parser.cwl_v1_0.CommandLineTool):
        cwl_utils.parser.cwl_v1_0_utils.convert_stdstreams_to_files(process)
    elif isinstance(process, cwl_utils.parser.cwl_v1_1.CommandLineTool):
        cwl_utils.parser.cwl_v1_1_utils.convert_stdstreams_to_files(process)
    elif isinstance(process, cwl_utils.parser.cwl_v1_2.CommandLineTool):
        cwl_utils.parser.cwl_v1_2_utils.convert_stdstreams_to_files(process)


def _create_command(
        cwl_element: cwl_utils.parser.CommandLineTool,
        cwl_name_prefix: str,
        schema_def_types: MutableMapping[str, Any],
        context: MutableMapping[str, Any],
        step: Step) -> CWLCommand:
    requirements = {**context['hints'], **context['requirements']}
    # Process ShellCommandRequirement
    is_shell_command = 'ShellCommandRequirement' in requirements
    # Create command
    command = CWLCommand(
        step=step,
        base_command=(cwl_element.baseCommand if isinstance(cwl_element.baseCommand, MutableSequence)
                      else ([cwl_element.baseCommand] if cwl_element.baseCommand is not None else [])),
        command_tokens=[_get_command_token(binding=a, is_shell_command=is_shell_command)
                        for a in (cwl_element.arguments or [])],
        success_codes=cwl_element.successCodes,
        failure_codes=(cwl_element.permanentFailCodes or []).extend(cwl_element.permanentFailCodes or []) or None,
        is_shell_command=is_shell_command,
        step_stdin=cwl_element.stdin,
        step_stdout=cwl_element.stdout,
        step_stderr=cwl_element.stderr)
    # Process InitialWorkDirRequirement
    if 'InitialWorkDirRequirement' in requirements:
        command.initial_work_dir = _inject_value(requirements['InitialWorkDirRequirement'].listing)
        command.absolute_initial_workdir_allowed = 'DockerRequirement' in context['requirements']
    # Process InplaceUpdateRequirement
    if 'InplaceUpdateRequirement' in requirements:
        command.inplace_update = requirements['InplaceUpdateRequirement'].inplaceUpdate
    # Process EnvVarRequirement
    if 'EnvVarRequirement' in requirements:
        for env_entry in requirements['EnvVarRequirement'].envDef:
            command.environment[env_entry.envName] = env_entry.envValue
    # Process inputs
    for input_port in cwl_element.inputs:
        command_token = _get_command_token_from_input(
            cwl_element=input_port,
            port_type=input_port.type,
            input_name=_get_name('', cwl_name_prefix, input_port.id),
            is_shell_command=command.is_shell_command,
            schema_def_types=schema_def_types)
        if command_token is not None:
            command.command_tokens.append(command_token)
    return command


def _create_command_output_processor(port_name: str,
                                     workflow: Workflow,
                                     port_target: Optional[Target],
                                     port_type: Any,
                                     cwl_element: Union[cwl_utils.parser.cwl_v1_0.CommandOutputParameter,
                                                        cwl_utils.parser.cwl_v1_1.CommandOutputParameter,
                                                        cwl_utils.parser.cwl_v1_2.CommandOutputParameter],
                                     cwl_name_prefix: str,
                                     schema_def_types: MutableMapping[str, Any],
                                     context: MutableMapping[str, Any],
                                     optional: bool = False) -> CommandOutputProcessor:
    # Array type: -> MapCommandOutputProcessor
    if (isinstance(port_type, cwl_utils.parser.cwl_v1_0.ArraySchema) or
            isinstance(port_type, cwl_utils.parser.cwl_v1_1.ArraySchema) or
            isinstance(port_type, cwl_utils.parser.cwl_v1_2.ArraySchema)):
        return CWLMapCommandOutputProcessor(
            name=port_name,
            workflow=workflow,
            processor=_create_command_output_processor(
                port_name=port_name,
                workflow=workflow,
                port_target=port_target,
                port_type=port_type.items,
                cwl_element=cwl_element,
                cwl_name_prefix=cwl_name_prefix,
                schema_def_types=schema_def_types,
                context=context,
                optional=optional))
    # Enum type: -> create command output processor
    elif (isinstance(port_type, cwl_utils.parser.cwl_v1_0.EnumSchema) or
          isinstance(port_type, cwl_utils.parser.cwl_v1_1.EnumSchema) or
          isinstance(port_type, cwl_utils.parser.cwl_v1_2.EnumSchema)):
        # Process InlineJavascriptRequirement
        requirements = {**context['hints'], **context['requirements']}
        expression_lib, full_js = _process_javascript_requirement(requirements)
        if type_name := getattr(port_type, 'name'):
            if type_name.startswith('_:'):
                enum_prefix = cwl_name_prefix
            else:
                enum_prefix = _get_name(posixpath.sep, posixpath.sep, type_name)
        else:
            enum_prefix = cwl_name_prefix
        # Return OutputProcessor
        return CWLCommandOutputProcessor(
            name=port_name,
            workflow=workflow,
            target=port_target,
            token_type=port_type.type,
            enum_symbols=[posixpath.relpath(_get_name(posixpath.sep, posixpath.sep, s), enum_prefix)
                          for s in port_type.symbols],
            expression_lib=expression_lib,
            full_js=full_js,
            optional=optional)
    # Record type: -> ObjectCommandOutputProcessor
    elif (isinstance(port_type, cwl_utils.parser.cwl_v1_0.RecordSchema) or
          isinstance(port_type, cwl_utils.parser.cwl_v1_1.RecordSchema) or
          isinstance(port_type, cwl_utils.parser.cwl_v1_2.RecordSchema)):
        # Process InlineJavascriptRequirement
        requirements = {**context['hints'], **context['requirements']}
        expression_lib, full_js = _process_javascript_requirement(requirements)
        # Create processor
        if (type_name := getattr(port_type, 'name', port_name)).startswith('_:'):
            type_name = port_name
        record_name_prefix = _get_name(posixpath.sep, posixpath.sep, type_name)
        return CWLObjectCommandOutputProcessor(
            name=port_name,
            workflow=workflow,
            processors={_get_name('', record_name_prefix, port_type.name): _create_command_output_processor(
                port_name=port_name,
                port_target=port_target,
                workflow=workflow,
                port_type=port_type.type,
                cwl_element=port_type,
                cwl_name_prefix=posixpath.join(record_name_prefix,
                                               _get_name('', record_name_prefix, port_type.name)),
                schema_def_types=schema_def_types,
                context=context) for port_type in port_type.fields},
            expression_lib=expression_lib,
            full_js=full_js,
            output_eval=cwl_element.outputBinding.outputEval if getattr(cwl_element, 'outputBinding', None) else None)
    elif isinstance(port_type, MutableSequence):
        optional = 'null' in port_type
        types = [t for t in filter(lambda x: x != 'null', port_type)]
        # Optional type (e.g. ['null', Type] -> Equivalent to Type?
        if len(types) == 1:
            return _create_command_output_processor(
                port_name=port_name,
                workflow=workflow,
                port_target=port_target,
                port_type=types[0],
                cwl_element=cwl_element,
                cwl_name_prefix=cwl_name_prefix,
                schema_def_types=schema_def_types,
                context=context,
                optional=optional)
        # List of types: -> UnionOutputProcessor
        else:
            return CWLUnionCommandOutputProcessor(
                name=port_name,
                workflow=workflow,
                processors=[_create_command_output_processor(
                    port_name=port_name,
                    workflow=workflow,
                    port_target=port_target,
                    port_type=port_type,
                    cwl_element=cwl_element,
                    cwl_name_prefix=cwl_name_prefix,
                    schema_def_types=schema_def_types,
                    context=context,
                    optional=optional) for port_type in types])
    # Complex type -> Extract from schema definitions and propagate
    elif '#' in port_type:
        return _create_command_output_processor(
            port_name=port_name,
            workflow=workflow,
            port_target=port_target,
            port_type=schema_def_types[port_type],
            cwl_element=cwl_element,
            cwl_name_prefix=cwl_name_prefix,
            schema_def_types=schema_def_types,
            context=context,
            optional=optional)
    # Simple type -> Create typed processor
    else:
        # Process InlineJavascriptRequirement
        requirements = {**context['hints'], **context['requirements']}
        expression_lib, full_js = _process_javascript_requirement(requirements)
        # Create OutputProcessor
        if port_type == 'File':
            return CWLCommandOutputProcessor(
                name=port_name,
                workflow=workflow,
                target=port_target,
                token_type=port_type,
                expression_lib=expression_lib,
                file_format=getattr(cwl_element, 'format', None),
                full_js=full_js,
                glob=(cwl_element.outputBinding.glob if getattr(cwl_element, 'outputBinding', None)
                      else getattr(cwl_element, 'path', None)),
                load_contents=_get_load_contents(cwl_element),
                load_listing=_get_load_listing(cwl_element, context),
                optional=optional,
                output_eval=(cwl_element.outputBinding.outputEval if getattr(cwl_element, 'outputBinding', None)
                             else None),
                secondary_files=_get_secondary_files(
                    cwl_element=getattr(cwl_element, 'secondaryFiles', None),
                    default_required=False),
                streamable=getattr(cwl_element, 'streamable', None))
        else:
            # Normalize port type (Python does not distinguish among all CWL number types)
            port_type = 'long' if port_type == 'int' else 'double' if port_type == 'float' else port_type
            return CWLCommandOutputProcessor(
                name=port_name,
                workflow=workflow,
                target=port_target,
                token_type=port_type,
                expression_lib=expression_lib,
                full_js=full_js,
                glob=cwl_element.outputBinding.glob if getattr(cwl_element, 'outputBinding', None) else None,
                load_contents=_get_load_contents(cwl_element),
                load_listing=_get_load_listing(cwl_element, context),
                optional=optional,
                output_eval=(cwl_element.outputBinding.outputEval if getattr(cwl_element, 'outputBinding', None)
                             else None))


def _create_context(version: str) -> MutableMapping[str, Any]:
    return {
        'default': {},
        'elements': {},
        'requirements': {},
        'hints': {},
        'version': version
    }


def _create_list_merger(name: str,
                        workflow: Workflow,
                        ports: MutableMapping[str, Port],
                        output_port: Optional[Port] = None,
                        link_merge: Optional[str] = None,
                        pick_value: Optional[str] = None) -> Step:
    combinator = workflow.create_step(
        cls=CombinatorStep,
        name=name + "-combinator",
        combinator=ListMergeCombinator(
            name=utils.random_name(),
            workflow=workflow,
            input_names=list(ports.keys()),
            output_name=name,
            flatten=(link_merge == 'merge_flattened')))
    for port_name, port in ports.items():
        combinator.add_input_port(port_name, port)
        combinator.combinator.add_item(port_name)
    if pick_value == 'first_non_null':
        combinator.add_output_port(name, workflow.create_port())
        transformer = workflow.create_step(
            cls=FirstNonNullTransformer,
            name=name + "-transformer")
        transformer.add_input_port(name, combinator.get_output_port())
        transformer.add_output_port(name, output_port or workflow.create_port())
        return transformer
    elif pick_value == 'the_only_non_null':
        combinator.add_output_port(name, workflow.create_port())
        transformer = workflow.create_step(
            cls=OnlyNonNullTransformer,
            name=name + "-transformer")
        transformer.add_input_port(name, combinator.get_output_port())
        transformer.add_output_port(name, output_port or workflow.create_port())
        return transformer
    elif pick_value == 'all_non_null':
        combinator.add_output_port(name, workflow.create_port())
        transformer = workflow.create_step(
            cls=AllNonNullTransformer,
            name=name + "-transformer")
        if link_merge is None:
            list_to_element = workflow.create_step(
                cls=ListToElementTransformer,
                name=name + "-list-to-element")
            list_to_element.add_input_port(name, combinator.get_output_port())
            list_to_element.add_output_port(name, workflow.create_port())
            transformer.add_input_port(name, list_to_element.get_output_port())
            transformer.add_output_port(name, output_port or workflow.create_port())
        else:
            transformer.add_input_port(name, combinator.get_output_port())
            transformer.add_output_port(name, output_port or workflow.create_port())
        return transformer
    elif link_merge is None:
        combinator.add_output_port(name, workflow.create_port())
        list_to_element = workflow.create_step(
            cls=ListToElementTransformer,
            name=name + "-list-to-element")
        list_to_element.add_input_port(name, combinator.get_output_port())
        list_to_element.add_output_port(name, output_port or workflow.create_port())
        return list_to_element
    else:
        combinator.add_output_port(name, output_port or workflow.create_port())
        return combinator


def _create_residual_combinator(workflow: Workflow,
                                step_name: str,
                                inner_combinator: Combinator,
                                inner_inputs: MutableSequence[str],
                                input_ports: MutableMapping[str, Port]) -> Combinator:
    dot_product_combinator = DotProductCombinator(
        workflow=workflow,
        name=step_name + "-dot-product-combinator")
    if inner_combinator:
        dot_product_combinator.add_combinator(
            inner_combinator, inner_combinator.get_items(recursive=True))
    else:
        for global_name in inner_inputs:
            dot_product_combinator.add_item(posixpath.relpath(global_name, step_name))
    for global_name in input_ports:
        if global_name not in inner_inputs:
            dot_product_combinator.add_item(posixpath.relpath(global_name, step_name))
    return dot_product_combinator


def _create_token_processor(port_name: str,
                            workflow: Workflow,
                            port_type: Any,
                            cwl_element: Union[cwl_utils.parser.cwl_v1_0.InputParameter,
                                               cwl_utils.parser.cwl_v1_1.InputParameter,
                                               cwl_utils.parser.cwl_v1_2.InputParameter,
                                               cwl_utils.parser.cwl_v1_0.OutputParameter,
                                               cwl_utils.parser.cwl_v1_1.OutputParameter,
                                               cwl_utils.parser.cwl_v1_2.OutputParameter,
                                               cwl_utils.parser.cwl_v1_0.WorkflowStepInput,
                                               cwl_utils.parser.cwl_v1_1.WorkflowStepInput,
                                               cwl_utils.parser.cwl_v1_2.WorkflowStepInput],
                            cwl_name_prefix: str,
                            schema_def_types: MutableMapping[str, Any],
                            format_graph: Graph,
                            context: MutableMapping[str, Any],
                            optional: bool = False,
                            default_required_sf: bool = True,
                            check_type: bool = True,
                            force_deep_listing: bool = False,
                            only_propagate_secondary_files: bool = True) -> TokenProcessor:
    # Array type: -> MapTokenProcessor
    if (isinstance(port_type, cwl_utils.parser.cwl_v1_0.ArraySchema) or
            isinstance(port_type, cwl_utils.parser.cwl_v1_1.ArraySchema) or
            isinstance(port_type, cwl_utils.parser.cwl_v1_2.ArraySchema)):
        return CWLMapTokenProcessor(
            name=port_name,
            workflow=workflow,
            processor=_create_token_processor(
                port_name=port_name,
                workflow=workflow,
                port_type=port_type.items,
                cwl_element=cwl_element,
                cwl_name_prefix=cwl_name_prefix,
                schema_def_types=schema_def_types,
                format_graph=format_graph,
                context=context,
                optional=optional,
                check_type=check_type,
                force_deep_listing=force_deep_listing,
                only_propagate_secondary_files=only_propagate_secondary_files))
    # Enum type: -> create output processor
    elif (isinstance(port_type, cwl_utils.parser.cwl_v1_0.EnumSchema) or
          isinstance(port_type, cwl_utils.parser.cwl_v1_1.EnumSchema) or
          isinstance(port_type, cwl_utils.parser.cwl_v1_2.EnumSchema)):
        # Process InlineJavascriptRequirement
        requirements = {**context['hints'], **context['requirements']}
        expression_lib, full_js = _process_javascript_requirement(requirements)
        if type_name := getattr(port_type, 'name'):
            if type_name.startswith('_:'):
                enum_prefix = cwl_name_prefix
            else:
                enum_prefix = _get_name(posixpath.sep, posixpath.sep, type_name)
        else:
            enum_prefix = cwl_name_prefix
        # Return TokenProcessor
        return CWLTokenProcessor(
            name=port_name,
            workflow=workflow,
            token_type=port_type.type,
            check_type=check_type,
            enum_symbols=[posixpath.relpath(_get_name(posixpath.sep, posixpath.sep, s), enum_prefix)
                          for s in port_type.symbols],
            expression_lib=expression_lib,
            full_js=full_js,
            optional=optional)
    # Record type: -> ObjectTokenProcessor
    elif (isinstance(port_type, cwl_utils.parser.cwl_v1_0.RecordSchema) or
          isinstance(port_type, cwl_utils.parser.cwl_v1_1.RecordSchema) or
          isinstance(port_type, cwl_utils.parser.cwl_v1_2.RecordSchema)):
        if (type_name := getattr(port_type, 'name', port_name)).startswith('_:'):
            type_name = port_name
        record_name_prefix = _get_name(posixpath.sep, posixpath.sep, type_name)
        return CWLObjectTokenProcessor(
            name=port_name,
            workflow=workflow,
            processors={_get_name('', record_name_prefix, port_type.name): _create_token_processor(
                port_name=port_name,
                workflow=workflow,
                port_type=port_type.type,
                cwl_element=port_type,
                cwl_name_prefix=posixpath.join(record_name_prefix,
                                               _get_name('', record_name_prefix, port_type.name)),
                schema_def_types=schema_def_types,
                format_graph=format_graph,
                context=context,
                check_type=check_type,
                force_deep_listing=force_deep_listing,
                only_propagate_secondary_files=only_propagate_secondary_files
            ) for port_type in port_type.fields})
    elif isinstance(port_type, MutableSequence):
        optional = 'null' in port_type
        types = [t for t in filter(lambda x: x != 'null', port_type)]
        # Optional type (e.g. ['null', Type] -> Equivalent to Type?
        if len(types) == 1:
            return _create_token_processor(
                port_name=port_name,
                workflow=workflow,
                port_type=types[0],
                cwl_element=cwl_element,
                cwl_name_prefix=cwl_name_prefix,
                schema_def_types=schema_def_types,
                format_graph=format_graph,
                context=context,
                optional=optional,
                check_type=check_type,
                force_deep_listing=force_deep_listing,
                only_propagate_secondary_files=only_propagate_secondary_files)
        # List of types: -> UnionOutputProcessor
        else:
            return CWLUnionTokenProcessor(
                name=port_name,
                workflow=workflow,
                processors=[_create_token_processor(
                    port_name=port_name,
                    workflow=workflow,
                    port_type=port_type,
                    cwl_element=cwl_element,
                    cwl_name_prefix=cwl_name_prefix,
                    schema_def_types=schema_def_types,
                    format_graph=format_graph,
                    context=context,
                    optional=optional,
                    check_type=check_type,
                    force_deep_listing=force_deep_listing,
                    only_propagate_secondary_files=only_propagate_secondary_files
                ) for port_type in types])
    # Complex type -> Extract from schema definitions and propagate
    elif '#' in port_type:
        return _create_token_processor(
            port_name=port_name,
            workflow=workflow,
            port_type=schema_def_types[port_type],
            cwl_element=cwl_element,
            cwl_name_prefix=cwl_name_prefix,
            schema_def_types=schema_def_types,
            format_graph=format_graph,
            context=context,
            optional=optional,
            check_type=check_type,
            force_deep_listing=force_deep_listing,
            only_propagate_secondary_files=only_propagate_secondary_files)
    # Simple type -> Create typed processor
    else:
        # Process InlineJavascriptRequirement
        requirements = {**context['hints'], **context['requirements']}
        expression_lib, full_js = _process_javascript_requirement(requirements)
        # Create OutputProcessor
        if port_type == 'File':
            return CWLTokenProcessor(
                name=port_name,
                workflow=workflow,
                token_type=port_type,
                check_type=check_type,
                expression_lib=expression_lib,
                file_format=getattr(cwl_element, 'format', None),
                format_graph=format_graph,
                full_js=full_js,
                load_contents=_get_load_contents(cwl_element),
                load_listing=(LoadListing.deep_listing if force_deep_listing else
                              _get_load_listing(cwl_element, context)),
                only_propagate_secondary_files=only_propagate_secondary_files,
                optional=optional,
                secondary_files=_get_secondary_files(
                    cwl_element=getattr(cwl_element, 'secondaryFiles', None),
                    default_required=default_required_sf),
                streamable=getattr(cwl_element, 'streamable', None))
        else:
            # Normalize port type (Python does not distinguish among all CWL number types)
            port_type = 'long' if port_type == 'int' else 'double' if port_type == 'float' else port_type
            return CWLTokenProcessor(
                name=port_name,
                workflow=workflow,
                token_type=port_type,
                check_type=check_type,
                expression_lib=expression_lib,
                full_js=full_js,
                load_contents=_get_load_contents(cwl_element),
                load_listing=(LoadListing.deep_listing if force_deep_listing else
                              _get_load_listing(cwl_element, context)),
                optional=optional)


def _create_token_transformer(name: str,
                              port_name: str,
                              workflow: Workflow,
                              cwl_element: Union[cwl_utils.parser.cwl_v1_0.InputParameter,
                                                 cwl_utils.parser.cwl_v1_1.InputParameter,
                                                 cwl_utils.parser.cwl_v1_2.InputParameter],
                              cwl_name_prefix: str,
                              schema_def_types: MutableMapping[str, Any],
                              format_graph: Graph,
                              context: MutableMapping[str, Any],
                              check_type: bool = True,
                              only_propagate_secondary_files: bool = True) -> CWLTokenTransformer:
    token_transformer = workflow.create_step(
        cls=CWLTokenTransformer,
        name=name,
        port_name=port_name,
        processor=_create_token_processor(
            port_name=port_name,
            workflow=workflow,
            port_type=cwl_element.type,
            cwl_element=cwl_element,
            cwl_name_prefix=cwl_name_prefix,
            schema_def_types=schema_def_types,
            format_graph=format_graph,
            context=context,
            check_type=check_type,
            only_propagate_secondary_files=only_propagate_secondary_files))
    token_transformer.add_output_port(port_name, workflow.create_port())
    return token_transformer


def _get_command_token(binding: Any,
                       value_from: Optional[Any] = None,
                       is_shell_command: bool = False,
                       input_name: Optional[str] = None,
                       token_type: Optional[str] = None) -> CWLCommandToken:
    # Normalize type (Python does not distinguish among all CWL number types)
    token_type = 'long' if token_type == 'int' else 'double' if token_type == 'float' else token_type
    if (isinstance(binding, cwl_utils.parser.cwl_v1_0.CommandLineBinding) or
            isinstance(binding, cwl_utils.parser.cwl_v1_1.CommandLineBinding) or
            isinstance(binding, cwl_utils.parser.cwl_v1_2.CommandLineBinding)):
        item_separator = binding.itemSeparator
        position = binding.position if binding.position is not None else 0
        prefix = binding.prefix
        separate = binding.separate if binding.separate is not None else True
        shell_quote = binding.shellQuote if binding.shellQuote is not None else True
        value = value_from or binding.valueFrom
        return CWLCommandToken(
            name=input_name,
            value=value,
            token_type=token_type,
            is_shell_command=is_shell_command,
            item_separator=item_separator,
            position=position,
            prefix=prefix,
            separate=separate,
            shell_quote=shell_quote)
    else:
        return CWLCommandToken(
            name=input_name,
            value=binding,
            token_type=token_type)


def _get_command_token_from_input(cwl_element: Any,
                                  port_type: Any,
                                  input_name: str,
                                  is_shell_command: bool = False,
                                  schema_def_types: Optional[MutableMapping[str, Any]] = None):
    token = None
    command_line_binding = cwl_element.inputBinding
    # Array type: -> CWLMapCommandToken
    if (isinstance(port_type, cwl_utils.parser.cwl_v1_0.ArraySchema) or
            isinstance(port_type, cwl_utils.parser.cwl_v1_1.ArraySchema) or
            isinstance(port_type, cwl_utils.parser.cwl_v1_2.ArraySchema)):
        token = _get_command_token_from_input(
            cwl_element=port_type,
            port_type=port_type.items,
            input_name=input_name,
            is_shell_command=is_shell_command,
            schema_def_types=schema_def_types)
        if token is not None:
            token = CWLMapCommandToken(
                name=input_name,
                value=token,
                is_shell_command=is_shell_command)
    # Enum type: -> substitute the type with string and reprocess
    if (isinstance(port_type, cwl_utils.parser.cwl_v1_0.EnumSchema) or
            isinstance(port_type, cwl_utils.parser.cwl_v1_1.EnumSchema) or
            isinstance(port_type, cwl_utils.parser.cwl_v1_2.EnumSchema)):
        return _get_command_token_from_input(
            cwl_element=cwl_element,
            port_type='string',
            input_name=input_name,
            is_shell_command=is_shell_command,
            schema_def_types=schema_def_types)
    # Object type: -> CWLObjectCommandToken
    if (isinstance(port_type, cwl_utils.parser.cwl_v1_0.RecordSchema) or
            isinstance(port_type, cwl_utils.parser.cwl_v1_1.RecordSchema) or
            isinstance(port_type, cwl_utils.parser.cwl_v1_2.RecordSchema)):
        if (type_name := getattr(port_type, 'name', input_name)).startswith('_:'):
            type_name = input_name
        record_name_prefix = _get_name(posixpath.sep, posixpath.sep, type_name)
        command_tokens = {}
        for el in port_type.fields:
            key = _get_name('', record_name_prefix, el.name)
            if (el_token := _get_command_token_from_input(
                    cwl_element=el,
                    port_type=el.type,
                    input_name=key,
                    is_shell_command=is_shell_command,
                    schema_def_types=schema_def_types)) is not None:
                command_tokens[key] = el_token
        if command_tokens:
            token = CWLObjectCommandToken(
                name=input_name,
                value=command_tokens,
                is_shell_command=True,
                shell_quote=False)
    elif isinstance(port_type, MutableSequence):
        types = [t for t in filter(lambda x: x != 'null', port_type)]
        # Optional type (e.g. ['null', Type] -> propagate
        if len(types) == 1:
            return _get_command_token_from_input(
                cwl_element=cwl_element,
                port_type=types[0],
                input_name=input_name,
                is_shell_command=is_shell_command,
                schema_def_types=schema_def_types)
        # List of types: -> CWLUnionCommandToken
        else:
            command_tokens = []
            for i, port_type in enumerate(types):
                token = _get_command_token_from_input(
                    cwl_element=cwl_element,
                    port_type=port_type,
                    input_name=input_name,
                    is_shell_command=is_shell_command,
                    schema_def_types=schema_def_types)
                if token is not None:
                    command_tokens.append(token)
            if command_tokens:
                token = CWLUnionCommandToken(
                    name=input_name,
                    value=command_tokens,
                    is_shell_command=True,
                    shell_quote=False)
    elif isinstance(port_type, str):
        # Complex type -> Extract from schema definitions and propagate
        if '#' in port_type:
            return _get_command_token_from_input(
                cwl_element=cwl_element,
                port_type=schema_def_types[port_type],
                input_name=input_name,
                is_shell_command=is_shell_command,
                schema_def_types=schema_def_types)
    # Simple type with `inputBinding` specified -> CWLCommandToken
    if command_line_binding is not None:
        if token is not None:
            # By default, do not escape composite command tokens
            if command_line_binding.shellQuote is None:
                command_line_binding.shellQuote = False
                return _get_command_token(
                    binding=command_line_binding,
                    value_from=token,
                    is_shell_command=True,
                    input_name=input_name)
            else:
                return _get_command_token(
                    binding=command_line_binding,
                    value_from=token,
                    is_shell_command=is_shell_command,
                    input_name=input_name)
        else:
            return _get_command_token(
                binding=command_line_binding,
                is_shell_command=is_shell_command,
                input_name=input_name,
                token_type=port_type)
    # Simple type without `inputBinding` specified -> token
    else:
        return token


def _get_hardware_requirement(
        cwl_version: str,
        requirements: MutableMapping[str, Any],
        expression_lib: Optional[MutableSequence[str]],
        full_js: bool):
    hardware_requirement = CWLHardwareRequirement(
        cwl_version=cwl_version,
        expression_lib=expression_lib,
        full_js=full_js)
    if 'ResourceRequirement' in requirements:
        resource_requirement = requirements['ResourceRequirement']
        hardware_requirement.cores = (
            resource_requirement.coresMin if resource_requirement.coresMin is not None
            else (resource_requirement.coresMax if resource_requirement.coresMax is not None
                  else hardware_requirement.cores))
        hardware_requirement.memory = (
            resource_requirement.ramMin if resource_requirement.ramMin is not None
            else (resource_requirement.ramMax if resource_requirement.ramMax is not None
                  else hardware_requirement.memory))
        hardware_requirement.tmpdir = (
            resource_requirement.tmpdirMin if resource_requirement.tmpdirMin is not None
            else (resource_requirement.tmpdirMax if resource_requirement.tmpdirMax is not None
                  else hardware_requirement.tmpdir))
        hardware_requirement.outdir = (
            resource_requirement.outdirMin if resource_requirement.outdirMin is not None
            else (resource_requirement.outdirMax if resource_requirement.outdirMax is not None
                  else hardware_requirement.outdir))
    return hardware_requirement


def _get_hint_object(cwl_version: str,
                     hint_map: MutableMapping[str, Any],
                     loadingOptions: cwl_utils.parser.LoadingOptions):
    mod = importlib.import_module('cwl_utils.parser.cwl_' + cwl_version.replace('.', '_'))
    class_ = getattr(mod, hint_map['class'], None)
    if class_:
        hint = getattr(mod, hint_map['class'])(
            loadingOptions=loadingOptions,
            **{k: v for k, v in hint_map.items() if k != 'class'})
        if isinstance(hint, cwl_utils.parser.cwl_v1_0.EnvVarRequirement):
            if isinstance(hint.envDef, MutableSequence):
                hint.envDef = [cwl_utils.parser.cwl_v1_0.EnvironmentDef(envName=ed['envName'], envValue=ed['envValue'])
                               for ed in hint.envDef]
            elif isinstance(hint, MutableMapping):
                hint.envDef = [cwl_utils.parser.cwl_v1_0.EnvironmentDef(
                    envName=k, envValue=v) for k, v in hint.items()]
        elif isinstance(hint, cwl_utils.parser.cwl_v1_1.EnvVarRequirement):
            if isinstance(hint.envDef, MutableSequence):
                hint.envDef = [cwl_utils.parser.cwl_v1_1.EnvironmentDef(envName=ed['envName'], envValue=ed['envValue'])
                               for ed in hint.envDef]
            elif isinstance(hint, MutableMapping):
                hint.envDef = [cwl_utils.parser.cwl_v1_1.EnvironmentDef(
                    envName=k, envValue=v) for k, v in hint.items()]
        elif isinstance(hint, cwl_utils.parser.cwl_v1_2.EnvVarRequirement):
            if isinstance(hint.envDef, MutableSequence):
                hint.envDef = [cwl_utils.parser.cwl_v1_1.EnvironmentDef(envName=ed['envName'], envValue=ed['envValue'])
                               for ed in hint.envDef]
            elif isinstance(hint, MutableMapping):
                hint.envDef = [cwl_utils.parser.cwl_v1_2.EnvironmentDef(
                    envName=k, envValue=v) for k, v in hint.items()]
        return hint
    else:
        return None


def _get_load_contents(port_description: Union[cwl_utils.parser.cwl_v1_0.InputParameter,
                                               cwl_utils.parser.cwl_v1_1.InputParameter,
                                               cwl_utils.parser.cwl_v1_2.InputParameter,
                                               cwl_utils.parser.cwl_v1_0.OutputParameter,
                                               cwl_utils.parser.cwl_v1_1.OutputParameter,
                                               cwl_utils.parser.cwl_v1_2.OutputParameter,
                                               cwl_utils.parser.cwl_v1_0.WorkflowStepInput,
                                               cwl_utils.parser.cwl_v1_1.WorkflowStepInput,
                                               cwl_utils.parser.cwl_v1_2.WorkflowStepInput]):
    if getattr(port_description, 'loadContents', None) is not None:
        return port_description.loadContents
    elif (getattr(port_description, 'inputBinding', None) and
          port_description.inputBinding.loadContents is not None):
        return port_description.inputBinding.loadContents
    elif (getattr(port_description, 'outputBinding', None) and
          port_description.outputBinding.loadContents is not None):
        return port_description.outputBinding.loadContents
    else:
        return None


def _get_load_listing(port_description: Union[cwl_utils.parser.cwl_v1_0.InputParameter,
                                              cwl_utils.parser.cwl_v1_1.InputParameter,
                                              cwl_utils.parser.cwl_v1_2.InputParameter,
                                              cwl_utils.parser.cwl_v1_0.OutputParameter,
                                              cwl_utils.parser.cwl_v1_1.OutputParameter,
                                              cwl_utils.parser.cwl_v1_2.OutputParameter,
                                              cwl_utils.parser.cwl_v1_0.WorkflowStepInput,
                                              cwl_utils.parser.cwl_v1_1.WorkflowStepInput,
                                              cwl_utils.parser.cwl_v1_2.WorkflowStepInput],
                      context: MutableMapping[str, Any]) -> LoadListing:
    requirements = {**context['hints'], **context['requirements']}
    if getattr(port_description, 'loadListing', None):
        return LoadListing[port_description.loadListing]
    elif (getattr(port_description, 'outputBinding', None) and
          getattr(port_description.outputBinding, 'loadListing', None) is not None):
        return LoadListing[port_description.outputBinding.loadListing]
    elif 'LoadListingRequirement' in requirements and requirements['LoadListingRequirement'].loadListing:
        return LoadListing[requirements['LoadListingRequirement'].loadListing]
    else:
        return LoadListing.deep_listing if context['version'] == 'v1.0' else LoadListing.no_listing


def _get_name(name_prefix: str,
              cwl_name_prefix: str,
              element_id: str,
              preserve_cwl_prefix: bool = False) -> str:
    name = element_id.split('#')[-1]
    return (posixpath.join(posixpath.sep, name) if preserve_cwl_prefix else posixpath.join(
        name_prefix, posixpath.relpath(posixpath.join(posixpath.sep, name), cwl_name_prefix)))


def _get_schema_def_types(requirements: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    return ({sd.name: sd for sd in requirements['SchemaDefRequirement'].types}
            if 'SchemaDefRequirement' in requirements else {})


def _get_secondary_files(cwl_element, default_required: bool) -> MutableSequence[SecondaryFile]:
    if not cwl_element:
        return []
    secondary_files = []
    for sf in cwl_element:
        if isinstance(sf, str):
            secondary_files.append(SecondaryFile(pattern=sf, required=default_required))
        elif (isinstance(sf, cwl_utils.parser.cwl_v1_1.SecondaryFileSchema) or
              isinstance(sf, cwl_utils.parser.cwl_v1_2.SecondaryFileSchema)):
            secondary_files.append(SecondaryFile(
                pattern=sf.pattern,
                required=sf.required if sf.required is not None else default_required))
    return secondary_files


def _inject_value(value: Any):
    if isinstance(value, MutableSequence):
        return [_inject_value(v) for v in value]
    elif isinstance(value, MutableMapping):
        if utils.get_token_class(value) is not None:
            return value
        else:
            return {k: _inject_value(v) for k, v in value.items()}
    elif (isinstance(value, cwl_utils.parser.cwl_v1_0.File) or
          isinstance(value, cwl_utils.parser.cwl_v1_1.File) or
          isinstance(value, cwl_utils.parser.cwl_v1_2.File)):
        dict_value = {'class': value.class_}
        if value.basename is not None:
            dict_value['basename'] = value.basename
        if value.checksum is not None:
            dict_value['checksum'] = value.checksum
        if value.contents is not None:
            dict_value['contents'] = value.contents
        if value.dirname is not None:
            dict_value['dirname'] = value.dirname
        if value.format is not None:
            dict_value['format'] = value.format
        if value.location is not None:
            dict_value['location'] = value.location
        if value.nameext is not None:
            dict_value['nameext'] = value.nameext
        if value.nameroot is not None:
            dict_value['nameroot'] = value.nameroot
        if value.path is not None:
            dict_value['path'] = value.path
        if value.secondaryFiles is not None:
            dict_value['secondaryFiles'] = [_inject_value(sf) for sf in value.secondaryFiles]
        if value.size is not None:
            dict_value['size'] = value.size
        return dict_value
    elif (isinstance(value, cwl_utils.parser.cwl_v1_0.Directory) or
          isinstance(value, cwl_utils.parser.cwl_v1_1.Directory) or
          isinstance(value, cwl_utils.parser.cwl_v1_2.Directory)):
        dict_value = {'class': value.class_}
        if value.basename is not None:
            dict_value['basename'] = value.basename
        if value.listing is not None:
            dict_value['listing'] = [_inject_value(sf) for sf in value.listing]
        if value.location is not None:
            dict_value['location'] = value.location
        if value.path is not None:
            dict_value['path'] = value.path
        return dict_value
    else:
        return value


def _get_workflow_step_input_type(element_input: Union[cwl_utils.parser.cwl_v1_0.WorkflowStepInput,
                                                       cwl_utils.parser.cwl_v1_1.WorkflowStepInput,
                                                       cwl_utils.parser.cwl_v1_2.WorkflowStepInput],
                                  workflow: cwl_utils.parser.Workflow):
    if not element_input.source:
        return 'Any'
    if isinstance(element_input, cwl_utils.parser.cwl_v1_0.WorkflowStepInput):
        return cwl_utils.parser.cwl_v1_0_utils.type_for_source(
            workflow, element_input.source)
    elif isinstance(element_input, cwl_utils.parser.cwl_v1_1.WorkflowStepInput):
        return cwl_utils.parser.cwl_v1_1_utils.type_for_source(
            workflow, element_input.source)
    elif isinstance(element_input, cwl_utils.parser.cwl_v1_2.WorkflowStepInput):
        return cwl_utils.parser.cwl_v1_2_utils.type_for_source(
            workflow, element_input.source)


def _is_command_line_tool(v: Any):
    return _is_instance(
        v,
        cwl_utils.parser.cwl_v1_0.CommandLineTool,
        cwl_utils.parser.cwl_v1_1.CommandLineTool,
        cwl_utils.parser.cwl_v1_2.CommandLineTool)


def _is_expression_tool(v: Any):
    return _is_instance(
        v,
        cwl_utils.parser.cwl_v1_0.ExpressionTool,
        cwl_utils.parser.cwl_v1_1.ExpressionTool,
        cwl_utils.parser.cwl_v1_2.ExpressionTool)


def _is_instance(v: Any, v1_0_type: Type, v1_1_type: Type, v1_2_type: Type):
    return isinstance(v, v1_0_type) or isinstance(v, v1_1_type) or isinstance(v, v1_2_type)


def _is_workflow(v: Any):
    return _is_instance(
        v,
        cwl_utils.parser.cwl_v1_0.Workflow,
        cwl_utils.parser.cwl_v1_1.Workflow,
        cwl_utils.parser.cwl_v1_2.Workflow)


def _is_workflow_step(v: Any):
    return _is_instance(
        v,
        cwl_utils.parser.cwl_v1_0.WorkflowStep,
        cwl_utils.parser.cwl_v1_1.WorkflowStep,
        cwl_utils.parser.cwl_v1_2.WorkflowStep)


def _percolate_port(port_name: str, *args) -> Port:
    for arg in args:
        if port_name in arg:
            port = arg[port_name]
            if isinstance(port, Port):
                return port
            else:
                return _percolate_port(port, *args)


def _process_docker_requirement(name: str,
                                target: Target,
                                context: MutableMapping[str, Any],
                                docker_requirement: Union[cwl_utils.parser.cwl_v1_0.DockerRequirement,
                                                          cwl_utils.parser.cwl_v1_1.DockerRequirement,
                                                          cwl_utils.parser.cwl_v1_2.DockerRequirement],
                                network_access: bool) -> Target:
    # Retrieve image
    if docker_requirement.dockerPull:
        image_name = docker_requirement.dockerPull
    elif docker_requirement.dockerImageId:
        image_name = docker_requirement.dockerImageId
    else:
        raise WorkflowDefinitionException(
            "DockerRequirements without `dockerPull` or `dockerImageId` are not supported yet")
    # Build configuration
    docker_config = {
        'image': image_name,
        'logDriver': 'none',
        'network': 'default' if network_access else 'none',
        'volume': ['{host}:{container}'.format(
            host=target.workdir,
            container=target.workdir)]}
    # Manage dockerOutputDirectory directive
    if docker_requirement.dockerOutputDirectory:
        docker_config['workdir'] = docker_requirement.dockerOutputDirectory
        context['output_directory'] = docker_config['workdir']
        local_dir = os.path.join(tempfile.gettempdir(), 'streamflow', random_name())
        os.makedirs(local_dir, exist_ok=True)
        docker_config['volume'].append('{host}:{container}'.format(
            host=local_dir,
            container=docker_config['workdir']))
    # Build step target
    deployment = DeploymentConfig(
        name=name,
        type='docker',
        config=docker_config)
    step_target = Target(
        deployment=deployment,
        service=image_name,
        workdir=target.workdir)
    return step_target


def _process_javascript_requirement(requirements: MutableMapping[str, Any]) -> (Optional[MutableSequence[Any]], bool):
    expression_lib = None
    full_js = False
    if 'InlineJavascriptRequirement' in requirements:
        full_js = True
        if requirements['InlineJavascriptRequirement'].expressionLib:
            expression_lib = []
            for lib in requirements['InlineJavascriptRequirement'].expressionLib:
                expression_lib.append(lib)
    return expression_lib, full_js


def _process_loop_transformers(step_name: str,
                               input_ports: MutableMapping[str, Port],
                               loop_input_ports: MutableMapping[str, Port],
                               transformers: MutableMapping[str, LoopValueFromTransformer],
                               input_dependencies: MutableMapping[str, Set[str]]):
    new_input_ports = {}
    for input_name, token_transformer in transformers.items():
        # Process inputs to attach ports
        for dep_name in input_dependencies[input_name]:
            if dep_name in input_ports:
                token_transformer.add_loop_input_port(
                    posixpath.relpath(dep_name, step_name), input_ports[dep_name])
        if input_name in loop_input_ports:
            token_transformer.add_loop_source_port(
                posixpath.relpath(input_name, step_name), loop_input_ports[input_name])
        # Put transformer output ports in input ports map
        new_input_ports[input_name] = token_transformer.get_output_port()
    return {**input_ports, **new_input_ports}


def _process_transformers(step_name: str,
                          input_ports: MutableMapping[str, Port],
                          transformers: MutableMapping[str, Transformer],
                          input_dependencies: MutableMapping[str, Set[str]]):
    new_input_ports = {}
    for input_name, token_transformer in transformers.items():
        # Process inputs to attach ports
        for dep_name in input_dependencies[input_name]:
            if dep_name in input_ports:
                token_transformer.add_input_port(
                    posixpath.relpath(dep_name, step_name), input_ports[dep_name])
        # Put transformer output ports in input ports map
        new_input_ports[input_name] = token_transformer.get_output_port()
    return {**input_ports, **new_input_ports}


class CWLTranslator(object):

    def __init__(self,
                 context: StreamFlowContext,
                 name: str,
                 output_directory: str,
                 cwl_definition: Union[cwl_utils.parser.CommandLineTool,
                                       cwl_utils.parser.ExpressionTool,
                                       cwl_utils.parser.Workflow],
                 cwl_inputs: MutableMapping[str, Any],
                 workflow_config: WorkflowConfig):
        self.context: StreamFlowContext = context
        self.name: str = name
        self.output_directory: str = output_directory
        self.cwl_definition: Union[cwl_utils.parser.CommandLineTool,
                                   cwl_utils.parser.ExpressionTool,
                                   cwl_utils.parser.Workflow] = cwl_definition
        self.cwl_inputs: MutableMapping[str, Any] = cwl_inputs
        self.default_map: MutableMapping[str, Any] = {}
        self.deployment_map: MutableMapping[str, DeployStep] = {}
        self.gather_map: MutableMapping[str, str] = {}
        self.input_ports: MutableMapping[str, Port] = {}
        self.output_ports: MutableMapping[str, Union[str, Port]] = {}
        self.scatter: MutableMapping[str, Any] = {}
        self.workflow_config: WorkflowConfig = workflow_config

    def _get_deploy_step(self,
                         target: Target,
                         workflow: Workflow):
        if target.deployment.name not in self.deployment_map:
            self.deployment_map[target.deployment.name] = workflow.create_step(
                cls=DeployStep,
                name=posixpath.join("__deploy__", target.deployment.name),
                deployment_config=target.deployment)
        return self.deployment_map[target.deployment.name]

    def _get_input_port(self,
                        workflow: Workflow,
                        cwl_element: Union[cwl_utils.parser.cwl_v1_0.Process,
                                           cwl_utils.parser.cwl_v1_1.Process,
                                           cwl_utils.parser.cwl_v1_2.Process],
                        element_input: Union[cwl_utils.parser.cwl_v1_0.InputParameter,
                                             cwl_utils.parser.cwl_v1_1.InputParameter,
                                             cwl_utils.parser.cwl_v1_2.InputParameter],
                        global_name: str,
                        port_name: str) -> Port:
        # Retrieve or create input port
        if global_name not in self.input_ports:
            self.input_ports[global_name] = workflow.create_port()
        input_port = self.input_ports[global_name]
        # If there is a default value, construct a default port block
        if element_input.default:
            # Insert default port
            transformer_suffix = ('-wf-default-transformer' if _is_workflow(cwl_element)
                                  else '-cmd-default-transformer')
            input_port = self._handle_default_port(
                global_name=global_name,
                port_name=port_name,
                transformer_suffix=transformer_suffix,
                port=input_port,
                workflow=workflow,
                value=element_input.default)
        # Return port
        return input_port

    def _get_source_port(self, workflow: Workflow, source_name: str) -> Port:
        if source_name not in self.input_ports:
            if source_name not in self.output_ports:
                self.output_ports[source_name] = workflow.create_port()
            return self.output_ports[source_name]
        else:
            return self.input_ports[source_name]

    def _get_target(self, name: str, target_type: str) -> Optional[Target]:
        path = PurePosixPath(name)
        target_config = self.workflow_config.propagate(path, target_type)
        if target_config is not None:
            if 'deployment' in target_config:
                target_deployment = self.workflow_config.deplyoments[target_config['deployment']]
            else:
                target_deployment = self.workflow_config.deplyoments[target_config['model']]
                logger.warn("The `model` keyword is deprecated and will be removed in StreamFlow 0.3.0. "
                            "Use `deployment` instead.")
            locations = target_config.get('locations', None)
            if locations is None:
                locations = target_config.get('resources')
                if locations is not None:
                    logger.warn("The `resources` keyword is deprecated and will be removed in StreamFlow 0.3.0. "
                                "Use `locations` instead.")
                else:
                    locations = 1
            deployment = DeploymentConfig(
                name=target_deployment['name'],
                type=target_deployment['type'],
                config=target_deployment['config'],
                external=target_deployment.get('external', False),
                lazy=target_deployment.get('lazy', True),
                workdir=target_deployment.get('workdir'))
            target = Target(
                deployment=deployment,
                locations=locations,
                service=target_config.get('service'),
                workdir=target_config.get('workdir'))
            return target

    def _handle_default_port(self,
                             global_name: str,
                             port_name: str,
                             transformer_suffix: str,
                             port: Optional[Port],
                             workflow: Workflow,
                             value: Any) -> Port:
        # Check output directory
        path = utils.get_path_from_id(self.cwl_definition.id)
        # Build default port
        default_port = workflow.create_port()
        self._inject_input(
            workflow=workflow,
            port_name="-".join([port_name, "default"]),
            global_name=global_name + transformer_suffix,
            port=default_port,
            workdir=os.path.dirname(path),
            value=value)
        if port is not None:
            # Add default transformer
            transformer = workflow.create_step(
                cls=CWLDefaultTransformer,
                name=global_name + transformer_suffix,
                default_port=default_port)
            transformer.add_input_port(port_name, port)
            transformer.add_output_port(port_name, workflow.create_port())
            return transformer.get_output_port()
        else:
            return default_port

    def _inject_input(self,
                      workflow: Workflow,
                      global_name: str,
                      port_name,
                      port: Port,
                      workdir: str,
                      value: Any) -> None:
        # Retrieve a local DeployStep
        target = self._get_target(global_name, 'port') or LocalTarget(workdir=workdir)
        deploy_step = self._get_deploy_step(target, workflow)
        # Create a schedule step and connect it to the local DeployStep
        schedule_step = workflow.create_step(
            cls=ScheduleStep,
            name=posixpath.join(global_name + "-injector", "__schedule__"),
            connector_port=deploy_step.get_output_port(),
            input_directory=workdir,
            output_directory=workdir,
            tmp_directory=tempfile.gettempdir(),
            target=target)
        # Create a CWLInputInjector step to process the input
        injector_step = workflow.create_step(
            cls=CWLInputInjectorStep,
            name=global_name + "-injector",
            job_port=schedule_step.get_output_port())
        # Create an input port and inject values
        input_port = workflow.create_port()
        input_port.put(Token(value=_inject_value(value)))
        input_port.put(TerminationToken())
        # Connect input and output ports to the injector step
        injector_step.add_input_port(port_name, input_port)
        injector_step.add_output_port(port_name, port)

    def _inject_inputs(self,
                       workflow: Workflow):
        output_directory = None
        if self.cwl_inputs:
            # Compute output directory path
            path = utils.get_path_from_id(self.cwl_inputs['id'])
            output_directory = os.path.dirname(path)
        # Compute suffix
        default_suffix = ('-wf-default-transformer' if _is_workflow(self.cwl_definition)
                          else '-cmd-default-transformer')
        # Process externally provided inputs
        for global_name in self.input_ports:
            if (step := workflow.steps.get(
                    global_name + default_suffix, workflow.steps.get(global_name + "-token-transformer"))):
                step_name = posixpath.dirname(global_name)
                port_name = posixpath.relpath(global_name, step_name)
                input_port = step.get_input_port(port_name)
                # If an input is given for port, inject it
                if step_name == posixpath.sep and port_name in self.cwl_inputs:
                    self._inject_input(
                        workflow=workflow,
                        global_name=global_name,
                        port_name=port_name,
                        port=input_port,
                        workdir=output_directory,
                        value=self.cwl_inputs[port_name])
        # Search empty unbound input ports
        for input_port in workflow.ports.values():
            if input_port.empty() and not input_port.get_input_steps():
                input_port.put(Token(value=None))
                input_port.put(TerminationToken())

    def _recursive_translate(self,
                             workflow: Workflow,
                             cwl_element: Union[cwl_utils.parser.CommandLineTool,
                                                cwl_utils.parser.ExpressionTool,
                                                cwl_utils.parser.Workflow,
                                                cwl_utils.parser.WorkflowStep],
                             context: MutableMapping[str, Any],
                             name_prefix: str,
                             cwl_name_prefix: str):
        # Update context
        current_context = copy.deepcopy(context)
        for hint in (cwl_element.hints or []):
            if not isinstance(hint, MutableMapping):
                current_context['hints'][hint.class_] = hint
        for requirement in (cwl_element.requirements or []):
            current_context['requirements'][requirement.class_] = requirement
        # In the root process, override requirements when provided in the input file
        if name_prefix == posixpath.sep:
            req_string = 'https://w3id.org/cwl/cwl#requirements'
            if req_string in self.cwl_inputs:
                if context['version'] == 'v1.0':
                    raise WorkflowDefinitionException(
                        "`cwl:requirements` in the input object is not part of CWL v1.0.")
                else:
                    for requirement in self.cwl_inputs[req_string]:
                        if requirement := _get_hint_object(context['version'], requirement, cwl_element.loadingOptions):
                            current_context['requirements'][requirement.class_] = requirement
        # Dispatch element
        if _is_workflow(cwl_element):
            self._translate_workflow(
                workflow=workflow,
                cwl_element=cwl_element,
                context=current_context,
                name_prefix=name_prefix,
                cwl_name_prefix=cwl_name_prefix)
        elif _is_workflow_step(cwl_element):
            self._translate_workflow_step(
                workflow=workflow,
                cwl_element=cwl_element,
                context=current_context,
                name_prefix=name_prefix,
                cwl_name_prefix=cwl_name_prefix)
        elif _is_command_line_tool(cwl_element):
            self._translate_command_line_tool(
                workflow=workflow,
                cwl_element=cwl_element,
                context=current_context,
                name_prefix=name_prefix,
                cwl_name_prefix=cwl_name_prefix)
        elif _is_expression_tool(cwl_element):
            self._translate_command_line_tool(
                workflow=workflow,
                cwl_element=cwl_element,
                context=current_context,
                name_prefix=name_prefix,
                cwl_name_prefix=cwl_name_prefix)
        else:
            raise WorkflowDefinitionException(
                "Definition of type " + type(cwl_element).__class__.__name__ + " not supported")

    def _translate_command_line_tool(self,
                                     workflow: Workflow,
                                     cwl_element: Union[cwl_utils.parser.CommandLineTool,
                                                        cwl_utils.parser.ExpressionTool],
                                     context: MutableMapping[str, Any],
                                     name_prefix: str,
                                     cwl_name_prefix: str):
        context['elements'][cwl_element.id] = cwl_element
        logger.debug("Translating {} {}".format(cwl_element.__class__.__name__, name_prefix))
        # Extract custom types if present
        requirements = {**context['hints'], **context['requirements']}
        schema_def_types = _get_schema_def_types(requirements)
        # Process InlineJavascriptRequirement
        expression_lib, full_js = _process_javascript_requirement(requirements)
        # Retrieve target
        target = self._get_target(name_prefix, 'step') or LocalTarget()
        # Process DockerRequirement
        if 'DockerRequirement' in requirements:
            network_access = (requirements['NetworkAccess'].networkAccess if 'NetworkAccess' in requirements
                              else False)
            if target.deployment.name == LOCAL_LOCATION:
                target = _process_docker_requirement(
                    name=posixpath.join(name_prefix, 'docker-requirement'),
                    target=target,
                    context=context,
                    docker_requirement=requirements['DockerRequirement'],
                    network_access=network_access)
        # Create DeployStep to initialise the execution environment
        deploy_step = self._get_deploy_step(target, workflow)
        # Create a schedule step and connect it to the DeployStep
        schedule_step = workflow.create_step(
            cls=ScheduleStep,
            name=posixpath.join(name_prefix, '__schedule__'),
            connector_port=deploy_step.get_output_port(),
            target=target,
            hardware_requirement=_get_hardware_requirement(
                cwl_version=context['version'],
                requirements=requirements,
                expression_lib=expression_lib,
                full_js=full_js),
            output_directory=context.get('output_directory'))
        # Create the ExecuteStep and connect it to the ScheduleStep
        step = workflow.create_step(
            cls=ExecuteStep,
            name=name_prefix,
            job_port=schedule_step.get_output_port())
        # Process inputs
        input_ports = {}
        token_transformers = []
        for element_input in cwl_element.inputs:
            global_name = _get_name(
                name_prefix, cwl_name_prefix, element_input.id)
            port_name = posixpath.relpath(global_name, name_prefix)
            # Retrieve or create input port
            input_port = self._get_input_port(
                workflow=workflow,
                cwl_element=cwl_element,
                element_input=element_input,
                global_name=global_name,
                port_name=port_name)
            # Add a token transformer step to process inputs
            token_transformer = _create_token_transformer(
                name=global_name + "-token-transformer",
                port_name=port_name,
                workflow=workflow,
                cwl_element=element_input,
                cwl_name_prefix=posixpath.join(cwl_name_prefix, port_name),
                schema_def_types=schema_def_types,
                format_graph=cwl_element.loadingOptions.graph,
                context=context,
                only_propagate_secondary_files=(name_prefix != '/'))
            # Add the output port as an input of the schedule step
            schedule_step.add_input_port(port_name, token_transformer.get_output_port())
            # Create a TransferStep
            transfer_step = workflow.create_step(
                cls=CWLTransferStep,
                name=posixpath.join(name_prefix, "__transfer__", port_name),
                job_port=schedule_step.get_output_port())
            transfer_step.add_input_port(port_name, token_transformer.get_output_port())
            transfer_step.add_output_port(port_name, workflow.create_port())
            # Connect the transfer step with the ExecuteStep
            step.add_input_port(port_name, transfer_step.get_output_port(port_name))
            # Store input port and token transformer
            input_ports[port_name] = input_port
            token_transformers.append(token_transformer)
        # Add input ports to token transformers
        for port_name, input_port in input_ports.items():
            for token_transformer in token_transformers:
                token_transformer.add_input_port(port_name, input_port)
        # Process outputs
        for element_output in cwl_element.outputs:
            global_name = _get_name(
                name_prefix, cwl_name_prefix, element_output.id)
            port_name = posixpath.relpath(global_name, name_prefix)
            # Retrieve or create output port
            if global_name not in self.output_ports:
                self.output_ports[global_name] = workflow.create_port()
            output_port = self.output_ports[global_name]
            # If the port is bound to a remote target, add the connector dependency
            if self.workflow_config.propagate(PurePosixPath(global_name), 'port'):
                port_target = self._get_target(global_name, 'port') or LocalTarget()
                output_deploy_step = self._get_deploy_step(port_target, workflow)
                step.add_input_port(port_name + '__connector__', output_deploy_step.get_output_port())
                step.output_connectors[port_name] = port_name + '__connector__'
            else:
                port_target = None
            # Add output port to ExecuteStep
            step.add_output_port(
                name=port_name,
                port=output_port,
                output_processor=_create_command_output_processor(
                    port_name=port_name,
                    workflow=workflow,
                    port_target=port_target,
                    port_type=element_output.type,
                    cwl_element=element_output,
                    cwl_name_prefix=posixpath.join(cwl_name_prefix, port_name),
                    schema_def_types=schema_def_types,
                    context=context))
        if _is_command_line_tool(cwl_element):
            # Process command
            step.command = _create_command(
                cwl_element=cwl_element,
                cwl_name_prefix=cwl_name_prefix,
                schema_def_types=schema_def_types,
                context=context,
                step=step)
            # Process ToolTimeLimit
            if 'ToolTimeLimit' in requirements:
                step.command.time_limit = requirements['ToolTimeLimit'].timelimit
                if step.command.time_limit < 0:
                    raise WorkflowDefinitionException('Invalid time limit for step {step}'.format(step=name_prefix))
        elif _is_expression_tool(cwl_element):
            step.command = CWLExpressionCommand(step, cwl_element.expression)
        # Add JS requirements
        step.command.expression_lib = expression_lib
        step.command.full_js = full_js

    def _translate_workflow(self,
                            workflow: Workflow,
                            cwl_element: cwl_utils.parser.Workflow,
                            context: MutableMapping[str, Any],
                            name_prefix: str,
                            cwl_name_prefix: str):
        context['elements'][cwl_element.id] = cwl_element
        step_name = name_prefix
        logger.debug("Translating Workflow {}".format(step_name))
        # Extract custom types if present
        requirements = {**context['hints'], **context['requirements']}
        schema_def_types = _get_schema_def_types(requirements)
        # Extract JavaScript requirements
        expression_lib, full_js = _process_javascript_requirement(requirements)
        # Process inputs to create steps
        input_ports = {}
        token_transformers = {}
        input_dependencies = {}
        for element_input in cwl_element.inputs:
            global_name = _get_name(step_name, cwl_name_prefix, element_input.id)
            port_name = posixpath.relpath(global_name, step_name)
            # Retrieve or create input port
            input_ports[global_name] = self._get_input_port(
                workflow=workflow,
                cwl_element=cwl_element,
                element_input=element_input,
                global_name=global_name,
                port_name=port_name)
            # Create token transformer step
            token_transformers[global_name] = _create_token_transformer(
                name=global_name + "-token-transformer",
                port_name=port_name,
                workflow=workflow,
                cwl_element=element_input,
                cwl_name_prefix=posixpath.join(cwl_name_prefix, port_name),
                schema_def_types=schema_def_types,
                format_graph=cwl_element.loadingOptions.graph,
                context=context,
                only_propagate_secondary_files=(name_prefix != '/'))
            # Process dependencies
            local_deps = resolve_dependencies(
                expression=element_input.format,
                full_js=full_js,
                expression_lib=expression_lib)
            for secondary_file in _get_secondary_files(element_input.secondaryFiles, True):
                local_deps.update(
                    resolve_dependencies(
                        expression=secondary_file.pattern,
                        full_js=full_js,
                        expression_lib=expression_lib),
                    resolve_dependencies(
                        expression=secondary_file.required,
                        full_js=full_js,
                        expression_lib=expression_lib))
            input_dependencies[global_name] = set.union(
                {global_name},
                {posixpath.join(step_name, d) for d in local_deps})
        # Process inputs again to attach ports
        input_ports = _process_transformers(
            step_name=step_name,
            input_ports=input_ports,
            transformers=token_transformers,
            input_dependencies=input_dependencies)
        # Save input ports in the global map
        for input_name in token_transformers:
            self.input_ports[input_name] = input_ports[input_name]
        # Process outputs
        for element_output in cwl_element.outputs:
            global_name = _get_name(name_prefix, cwl_name_prefix, element_output.id)
            # If outputSource element is a list, the output element can depend on multiple ports
            if isinstance(element_output.outputSource, MutableSequence):
                # If the list contains only one element and no `linkMerge` or `pickValue` are specified
                if (len(element_output.outputSource) == 1 and
                        not getattr(element_output, 'linkMerge', None) and
                        not getattr(element_output, 'pickValue', None)):
                    # Treat it as a singleton
                    source_name = _get_name(
                        name_prefix, cwl_name_prefix, element_output.outputSource[0])
                    # If the output source is an input port, link the output to the input
                    if source_name in self.input_ports:
                        self.output_ports[global_name] = self.input_ports[source_name]
                    # Otherwise, simply propagate the output port
                    else:
                        self.output_ports[source_name] = self._get_source_port(workflow, global_name)
                # Otherwise, create a ListMergeCombinator
                else:
                    source_names = [_get_name(name_prefix, cwl_name_prefix, src)
                                    for src in element_output.outputSource]
                    ports = {n: self._get_source_port(workflow, n) for n in source_names}
                    _create_list_merger(
                        name=global_name,
                        workflow=workflow,
                        ports=ports,
                        output_port=self._get_source_port(workflow, global_name),
                        link_merge=getattr(element_output, 'linkMerge', None),
                        pick_value=getattr(element_output, 'pickValue', None))
            # Otherwise, the output element depends on a single output port
            else:
                source_name = _get_name(
                    name_prefix, cwl_name_prefix, element_output.outputSource)
                # If `pickValue` is specified, create a ListMergeCombinator
                if getattr(element_output, 'pickValue', None):
                    source_port = self._get_source_port(workflow, source_name)
                    _create_list_merger(
                        name=global_name,
                        workflow=workflow,
                        ports={source_name: source_port},
                        output_port=self._get_source_port(workflow, global_name),
                        link_merge=getattr(element_output, 'linkMerge', None),
                        pick_value=getattr(element_output, 'pickValue', None))
                else:
                    # If the output source is an input port, link the output to the input
                    if source_name in self.input_ports:
                        self.output_ports[global_name] = self.input_ports[source_name]
                    # Otherwise, simply propagate the output port
                    else:
                        self.output_ports[source_name] = self._get_source_port(workflow, global_name)
        # Process steps
        for step in cwl_element.steps:
            self._recursive_translate(
                workflow=workflow,
                cwl_element=step,
                context=context,
                name_prefix=name_prefix,
                cwl_name_prefix=cwl_name_prefix)

    def _translate_workflow_step(self,
                                 workflow: Workflow,
                                 cwl_element: cwl_utils.parser.WorkflowStep,
                                 context: MutableMapping[str, Any],
                                 name_prefix: str,
                                 cwl_name_prefix: str):
        # Process content
        context['elements'][cwl_element.id] = cwl_element
        step_name = _get_name(name_prefix, cwl_name_prefix, cwl_element.id)
        logger.debug("Translating WorkflowStep {}".format(step_name))
        cwl_step_name = _get_name(
            name_prefix, cwl_name_prefix, cwl_element.id, preserve_cwl_prefix=True)
        # Extract requirements
        for hint in (cwl_element.hints or []):
            if not isinstance(hint, MutableMapping):
                context['hints'][hint.class_] = hint
        for requirement in (cwl_element.requirements or []):
            context['requirements'][requirement.class_] = requirement
        requirements = {**context['hints'], **context['requirements']}
        # Extract JavaScript requirements
        expression_lib, full_js = _process_javascript_requirement(requirements)
        # Find scatter elements
        if isinstance(cwl_element.scatter, str):
            scatter_inputs = [_get_name(
                step_name, cwl_step_name, cwl_element.scatter)]
        else:
            scatter_inputs = [_get_name(step_name, cwl_step_name, n)
                              for n in (cwl_element.scatter or [])]
        # Process inputs
        input_ports = {}
        value_from_transformers = {}
        input_dependencies = {}
        for element_input in cwl_element.in_:
            self._translate_workflow_step_input(
                workflow=workflow,
                context=context,
                element_id=cwl_element.id,
                element_input=element_input,
                name_prefix=name_prefix,
                cwl_name_prefix=cwl_name_prefix,
                requirements=requirements,
                input_ports=input_ports,
                value_from_transformers=value_from_transformers,
                input_dependencies=input_dependencies,
                scatter_inputs=scatter_inputs)
        # Process loop inputs
        element_requirements = {
            **{h['class']: h for h in (cwl_element.hints or [])},
            **{r.class_: r for r in (cwl_element.requirements or [])}}
        if 'http://commonwl.org/cwltool#Loop' in element_requirements:
            loop_requirement = element_requirements['http://commonwl.org/cwltool#Loop']
            # Validate loop requirement
            if getattr(cwl_element, 'when', None):
                raise WorkflowDefinitionException(
                    'cwltool:Loop clause is not compatible with the `when` directive')
            if scatter_inputs:
                raise WorkflowDefinitionException(
                    'cwltool:Loop requirement is not compatible with the `scatter` directive')
            if 'loopWhen' not in loop_requirement:
                raise WorkflowDefinitionException(
                    'The `loopWhen` is required for cwltool:Loop requirement')
            # Build combinator
            loop_combinator = LoopCombinator(
                workflow=workflow,
                name=step_name + "-loop-combinator")
            for global_name in input_ports:
                # Decouple loop ports through a forwarder
                port_name = posixpath.relpath(global_name, step_name)
                loop_forwarder = workflow.create_step(
                    cls=ForwardTransformer,
                    name=global_name + "-input-forward-transformer")
                loop_forwarder.add_input_port(port_name, input_ports[global_name])
                input_ports[global_name] = workflow.create_port()
                loop_forwarder.add_output_port(port_name, input_ports[global_name])
                # Add item to combinator
                loop_combinator.add_item(posixpath.relpath(global_name, step_name))
            # Create a combinator step and add all inputs to it
            combinator_step = workflow.create_step(
                cls=LoopCombinatorStep,
                name=step_name + "-loop-combinator",
                combinator=loop_combinator)
            for global_name in input_ports:
                port_name = posixpath.relpath(global_name, step_name)
                combinator_step.add_input_port(port_name, input_ports[global_name])
                combinator_step.add_output_port(port_name, workflow.create_port())
            # Create loop conditional step
            loop_conditional_step = workflow.create_step(
                cls=CWLLoopConditionalStep,
                name=step_name + "-loop-when",
                expression=loop_requirement['loopWhen'],
                expression_lib=expression_lib,
                full_js=full_js)
            # Add inputs to conditional step
            for global_name in input_ports:
                port_name = posixpath.relpath(global_name, step_name)
                loop_conditional_step.add_input_port(port_name, combinator_step.get_output_port(port_name))
                input_ports[global_name] = workflow.create_port()
                loop_conditional_step.add_output_port(port_name, input_ports[global_name])
        # Retrieve scatter method (default to dotproduct)
        scatter_method = cwl_element.scatterMethod or 'dotproduct'
        # If there are scatter inputs
        if scatter_inputs:
            # If any scatter input is null, propagate an empty array on the output ports
            empty_scatter_conditional_step = workflow.create_step(
                cls=CWLEmptyScatterConditionalStep,
                name=step_name + "-empty-scatter-condition",
                scatter_method=scatter_method)
            for global_name in scatter_inputs:
                port_name = posixpath.relpath(global_name, step_name)
                empty_scatter_conditional_step.add_input_port(port_name, input_ports[global_name])
                input_ports[global_name] = workflow.create_port()
                empty_scatter_conditional_step.add_output_port(port_name, input_ports[global_name])
            # If there are multiple scatter inputs, configure combinator
            scatter_combinator = None
            if len(scatter_inputs) > 1:
                # Build combinator
                if scatter_method == 'dotproduct':
                    scatter_combinator = DotProductCombinator(
                        workflow=workflow,
                        name=step_name + "-scatter-combinator")
                    for global_name in scatter_inputs:
                        scatter_combinator.add_item(posixpath.relpath(global_name, step_name))
                else:
                    scatter_combinator = CartesianProductCombinator(
                        workflow=workflow,
                        name=step_name + "-scatter-combinator")
                    for global_name in scatter_inputs:
                        scatter_combinator.add_item(posixpath.relpath(global_name, step_name))
            # If there are both scatter and non-scatter inputs
            if len(scatter_inputs) < len(input_ports):
                scatter_combinator = _create_residual_combinator(
                    workflow=workflow,
                    step_name=step_name,
                    inner_combinator=scatter_combinator,
                    inner_inputs=scatter_inputs,
                    input_ports=input_ports)
            # If there are scatter inputs, process them
            for global_name in scatter_inputs:
                port_name = posixpath.relpath(global_name, step_name)
                scatter_step = workflow.create_step(
                    cls=ScatterStep,
                    name=global_name + "-scatter")
                scatter_step.add_input_port(port_name, input_ports[global_name])
                input_ports[global_name] = workflow.create_port()
                scatter_step.add_output_port(port_name, input_ports[global_name])
            # If there is a scatter combinator, create a combinator step and add all inputs to it
            if scatter_combinator:
                combinator_step = workflow.create_step(
                    cls=CombinatorStep,
                    name=step_name + "-scatter-combinator",
                    combinator=scatter_combinator)
                for global_name in input_ports:
                    port_name = posixpath.relpath(global_name, step_name)
                    combinator_step.add_input_port(port_name, input_ports[global_name])
                    input_ports[global_name] = workflow.create_port()
                    combinator_step.add_output_port(port_name, input_ports[global_name])
        # Process inputs again to attach ports to transformers
        input_ports = _process_transformers(
            step_name=step_name,
            input_ports=input_ports,
            transformers=value_from_transformers,
            input_dependencies=input_dependencies)
        # Save input ports in the global map
        self.input_ports = {**self.input_ports, **input_ports}
        # Process condition
        conditional_step = None
        if getattr(cwl_element, 'when', None):
            # Create conditional step
            conditional_step = workflow.create_step(
                cls=CWLConditionalStep,
                name=step_name + "-when",
                expression=cwl_element.when,
                expression_lib=expression_lib,
                full_js=full_js)
            # Add inputs and outputs to conditional step
            for global_name in input_ports:
                port_name = posixpath.relpath(global_name, step_name)
                conditional_step.add_input_port(port_name, self.input_ports[global_name])
                self.input_ports[global_name] = workflow.create_port()
                conditional_step.add_output_port(port_name, self.input_ports[global_name])
        # Process outputs
        external_output_ports = {}
        internal_output_ports = {}
        for element_output in cwl_element.out:
            global_name = _get_name(
                step_name, cwl_step_name, element_output)
            port_name = posixpath.relpath(global_name, step_name)
            # Retrieve or create output port
            if global_name not in self.output_ports:
                self.output_ports[global_name] = workflow.create_port()
            external_output_ports[global_name] = self.output_ports[global_name]
            internal_output_ports[global_name] = self.output_ports[global_name]
            # If there are scatter inputs
            if scatter_inputs:
                # Perform a gather on the outputs
                if scatter_method == 'nested_crossproduct':
                    gather_steps = []
                    internal_output_ports[global_name] = workflow.create_port()
                    gather_input_port = internal_output_ports[global_name]
                    for scatter_input in scatter_inputs:
                        scatter_port_name = posixpath.relpath(scatter_input, step_name)
                        gather_steps.append(workflow.create_step(
                            cls=GatherStep,
                            name=global_name + "-gather-" + scatter_port_name))
                        gather_steps[-1].add_input_port(port_name, gather_input_port)
                        gather_steps[-1].add_output_port(
                            name=port_name,
                            port=(external_output_ports[global_name] if len(gather_steps) == len(scatter_inputs)
                                  else workflow.create_port()))
                        gather_input_port = gather_steps[-1].get_output_port()
                else:
                    gather_step = workflow.create_step(
                        cls=GatherStep,
                        name=global_name + "-gather",
                        depth=1 if scatter_method == 'dotproduct' else len(scatter_inputs))
                    internal_output_ports[global_name] = workflow.create_port()
                    gather_step.add_input_port(port_name, internal_output_ports[global_name])
                    gather_step.add_output_port(port_name, external_output_ports[global_name])
                # Add the output port as a skip port in the empty scatter conditional step
                empty_scatter_conditional_step = cast(
                    CWLConditionalStep, workflow.steps[step_name + "-empty-scatter-condition"])
                empty_scatter_conditional_step.add_skip_port(port_name, external_output_ports[global_name])
            # Add skip ports if there is a condition
            if getattr(cwl_element, 'when', None):
                cast(CWLConditionalStep, conditional_step).add_skip_port(
                    port_name, internal_output_ports[global_name])
        # Process loop outputs
        if 'http://commonwl.org/cwltool#Loop' in element_requirements:
            loop_requirement = element_requirements['http://commonwl.org/cwltool#Loop']
            output_method = loop_requirement.get('outputMethod', 'last')
            # Retreive loop steps
            loop_conditional_step = cast(CWLLoopConditionalStep, workflow.steps[step_name + "-loop-when"])
            combinator_step = workflow.steps[step_name + "-loop-combinator"]
            # Create a loop termination combinator
            loop_terminator_combinator = LoopTerminationCombinator(
                workflow=workflow,
                name=step_name + "-loop-termination-combinator")
            loop_terminator_step = workflow.create_step(
                cls=CombinatorStep,
                name=step_name + "-loop-terminator",
                combinator=loop_terminator_combinator)
            for port_name, port in combinator_step.get_input_ports().items():
                loop_terminator_step.add_output_port(port_name, port)
                loop_terminator_combinator.add_output_item(port_name)
            # Add outputs to conditional step
            for global_name in internal_output_ports:
                port_name = posixpath.relpath(global_name, step_name)
                # Create loop forwarder
                loop_forwarder = workflow.create_step(
                    cls=ForwardTransformer,
                    name=global_name + "-output-forward-transformer")
                internal_output_ports[global_name] = workflow.create_port()
                loop_forwarder.add_input_port(port_name, internal_output_ports[global_name])
                self.output_ports[global_name] = workflow.create_port()
                loop_forwarder.add_output_port(port_name, self.output_ports[global_name])
                # Create loop output step
                loop_output_step = workflow.create_step(
                    cls=CWLLoopOutputLastStep if output_method == 'last' else CWLLoopOutputAllStep,
                    name=global_name + "-loop-output")
                loop_output_step.add_input_port(port_name, loop_forwarder.get_output_port())
                loop_conditional_step.add_skip_port(port_name, loop_forwarder.get_output_port())
                loop_output_step.add_output_port(port_name, external_output_ports[global_name])
                loop_terminator_step.add_input_port(port_name, external_output_ports[global_name])
                loop_terminator_combinator.add_item(port_name)
            # Process inputs
            loop_input_ports = {}
            loop_value_from_transformers = {}
            loop_input_dependencies = {}
            for loop_input in loop_requirement.get('loop', []):
                # Pre-process the `loopSource` field to avoid inconsistencies
                loop_input['source'] = loop_input.get('loopSource', loop_input['id'])
                self._translate_workflow_step_input(
                    workflow=workflow,
                    context=context,
                    element_id=cwl_element.id,
                    element_input=loop_input,
                    name_prefix=name_prefix,
                    cwl_name_prefix=cwl_name_prefix,
                    requirements=requirements,
                    input_ports=loop_input_ports,
                    value_from_transformers=loop_value_from_transformers,
                    input_dependencies=loop_input_dependencies,
                    scatter_inputs=scatter_inputs,
                    inner_steps_prefix='-loop',
                    value_from_transformer_cls=LoopValueFromTransformer)
            # Process inputs again to attach ports to transformers
            loop_input_ports = _process_loop_transformers(
                step_name=step_name,
                input_ports=input_ports,
                loop_input_ports=loop_input_ports,
                transformers=loop_value_from_transformers,
                input_dependencies=loop_input_dependencies)
            # Connect loop outputs to loop inputs
            for global_name in input_ports:
                # Create loop output step
                port_name = posixpath.relpath(global_name, step_name)
                loop_forwarder = workflow.create_step(
                    cls=ForwardTransformer,
                    name=global_name + "-output-forward-transformer")
                loop_forwarder.add_input_port(
                    port_name, loop_input_ports.get(global_name, loop_conditional_step.get_output_port(port_name)))
                loop_forwarder.add_output_port(port_name, combinator_step.get_input_port(port_name))
        # Add skip ports if there is a condition
        if getattr(cwl_element, 'when', None):
            for element_output in cwl_element.out:
                global_name = _get_name(step_name, cwl_step_name, element_output)
                port_name = posixpath.relpath(global_name, step_name)
                skip_port = (external_output_ports[global_name]
                             if 'http://commonwl.org/cwltool#Loop' in element_requirements
                             else internal_output_ports[global_name])
                cast(CWLConditionalStep, conditional_step).add_skip_port(port_name, skip_port)
        # Update output ports with the internal ones
        self.output_ports = {**self.output_ports, **internal_output_ports}
        # Process inner element
        run_command = cwl_element.run
        if cwl_utils.parser.is_process(run_command):
            _convert_stdstreams_to_files(run_command)
            if ':' in run_command.id.split('#')[-1]:
                inner_cwl_name_prefix = step_name
            else:
                inner_cwl_name_prefix = _get_name(
                    name_prefix, cwl_name_prefix, run_command.id, preserve_cwl_prefix=True)
        else:
            run_command = cwl_element.loadingOptions.fetcher.urljoin(
                cwl_element.loadingOptions.fileuri, run_command)
            run_command = cwl_utils.parser.load_document_by_uri(
                run_command, loadingOptions=cwl_element.loadingOptions)
            _convert_stdstreams_to_files(run_command)
            inner_cwl_name_prefix = (_get_name(posixpath.sep, posixpath.sep, run_command.id)
                                     if '#' in run_command.id else posixpath.sep)
            context = {**context, **{'version': run_command.cwlVersion}}
        self._recursive_translate(
            workflow=workflow,
            cwl_element=run_command,
            context=context,
            name_prefix=step_name,
            cwl_name_prefix=inner_cwl_name_prefix)
        # Update output ports with the external ones
        self.output_ports = {**self.output_ports, **external_output_ports}

    def _translate_workflow_step_input(self,
                                       workflow: Workflow,
                                       context: MutableMapping[str, Any],
                                       element_id: str,
                                       element_input: Union[cwl_utils.parser.cwl_v1_0.WorkflowStepInput,
                                                            cwl_utils.parser.cwl_v1_1.WorkflowStepInput,
                                                            cwl_utils.parser.cwl_v1_2.WorkflowStepInput],
                                       name_prefix: str,
                                       cwl_name_prefix: str,
                                       requirements: MutableMapping[str, Any],
                                       input_ports: MutableMapping[str, Port],
                                       value_from_transformers: MutableMapping[str, ValueFromTransformer],
                                       input_dependencies: MutableMapping[str, Any],
                                       scatter_inputs: MutableSequence[str, Any],
                                       inner_steps_prefix: str = '',
                                       value_from_transformer_cls: Type[ValueFromTransformer] = ValueFromTransformer):
        # Extract custom types if present
        schema_def_types = _get_schema_def_types(requirements)
        # Extract JavaScript requirements
        expression_lib, full_js = _process_javascript_requirement(requirements)
        # Extract names
        step_name = _get_name(name_prefix, cwl_name_prefix, element_id)
        cwl_step_name = _get_name(
            name_prefix, cwl_name_prefix, element_id, preserve_cwl_prefix=True)
        global_name = _get_name(
            step_name, cwl_step_name, element_input.id)
        port_name = posixpath.relpath(global_name, step_name)
        # If element contains `valueFrom` directive
        if element_input.valueFrom:
            prefix, suffix = element_id.split('#')
            if '/' in suffix:
                workflow_id = '#'.join([prefix, '/'.join(suffix.split('/')[:-1])])
            else:
                workflow_id = prefix
            # Create a ValueFromTransformer
            port_type = _get_workflow_step_input_type(
                element_input, context['elements'][workflow_id])
            if global_name in scatter_inputs:
                port_type = port_type.items
            value_from_transformers[global_name] = workflow.create_step(
                cls=value_from_transformer_cls,
                name=global_name + inner_steps_prefix + "-value-from-transformer",
                processor=_create_token_processor(
                    port_name=port_name,
                    workflow=workflow,
                    port_type=port_type,
                    cwl_element=element_input,
                    cwl_name_prefix=posixpath.join(cwl_step_name, port_name),
                    schema_def_types=schema_def_types,
                    format_graph=element_input.loadingOptions.graph,
                    context=context,
                    check_type=False),
                port_name=port_name,
                expression_lib=expression_lib,
                full_js=full_js,
                value_from=element_input.valueFrom)
            value_from_transformers[global_name].add_output_port(port_name, workflow.create_port())
            # Retrieve dependencies
            local_deps = resolve_dependencies(
                expression=element_input.valueFrom,
                full_js=full_js,
                expression_lib=expression_lib)
            input_dependencies[global_name] = set.union(
                {global_name},
                {posixpath.join(step_name, d) for d in local_deps})
        # If `source` entry is present, process output dependencies
        if element_input.source:
            # If source element is a list, the input element can depend on multiple ports
            if isinstance(element_input.source, MutableSequence):
                # If the list contains only one element and no `linkMerge` is specified, treat it as a singleton
                if (len(element_input.source) == 1 and
                        not getattr(element_input, 'linkMerge', None) and
                        not getattr(element_input, 'pickValue', None)):
                    source_name = _get_name(
                        name_prefix, cwl_name_prefix, element_input.source[0])
                    source_port = self._get_source_port(workflow, source_name)
                    # If there is a default value, construct a default port block
                    if element_input.default:
                        # Insert default port
                        source_port = self._handle_default_port(
                            global_name=global_name,
                            port_name=port_name,
                            transformer_suffix=inner_steps_prefix + "-step-default-transformer",
                            port=source_port,
                            workflow=workflow,
                            value=element_input.default)
                    # Add source port to the list of input ports for the current step
                    input_ports[global_name] = source_port
                # Otherwise, create a ListMergeCombinator
                else:
                    source_names = [_get_name(name_prefix, cwl_name_prefix, src)
                                    for src in element_input.source]
                    ports = {n: self._get_source_port(workflow, n) for n in source_names}
                    list_merger = _create_list_merger(
                        name=global_name + inner_steps_prefix + "-list-merge-combinator",
                        workflow=workflow,
                        ports=ports,
                        link_merge=getattr(element_input, 'linkMerge', None),
                        pick_value=getattr(element_input, 'pickValue', None))
                    # Add ListMergeCombinator output port to the list of input ports for the current step
                    input_ports[global_name] = list_merger.get_output_port()
            # Otherwise, the input element depends on a single output port
            else:
                source_name = _get_name(
                    name_prefix, cwl_name_prefix, cast(str, element_input.source))
                source_port = self._get_source_port(workflow, source_name)
                # If there is a default value, construct a default port block
                if element_input.default:
                    # Insert default port
                    source_port = self._handle_default_port(
                        global_name=global_name,
                        port_name=port_name,
                        transformer_suffix=inner_steps_prefix + "-step-default-transformer",
                        port=source_port,
                        workflow=workflow,
                        value=element_input.default)
                # Add source port to the list of input ports for the current step
                input_ports[global_name] = source_port
        # Otherwise, search for default values
        elif element_input.default:
            input_ports[global_name] = self._handle_default_port(
                global_name=global_name,
                port_name=port_name,
                transformer_suffix=inner_steps_prefix + "-step-default-transformer",
                port=None,
                workflow=workflow,
                value=element_input.default)
        # Otherwise, inject a synthetic port into the workflow
        else:
            input_ports[global_name] = workflow.create_port()

    def translate(self) -> Workflow:
        # Parse streams
        _convert_stdstreams_to_files(self.cwl_definition)
        # Create workflow
        workflow = Workflow(
            context=self.context,
            type='cwl',
            name=self.name)
        # Create context
        context = _create_context(version=self.cwl_definition.cwlVersion)
        # Compute root prefix
        workflow_id = self.cwl_definition.id
        context['elements'][workflow_id] = self.cwl_definition
        cwl_root_prefix = (_get_name(posixpath.sep, posixpath.sep, workflow_id) if '#' in workflow_id
                           else posixpath.sep)
        # Register data locations for config files
        path = utils.get_path_from_id(self.cwl_definition.id)
        self.context.data_manager.register_path(
            deployment=LOCAL_LOCATION,
            location=LOCAL_LOCATION,
            path=path,
            relpath=os.path.basename(path))
        if self.cwl_inputs:
            path = utils.get_path_from_id(self.cwl_inputs['id'])
            self.context.data_manager.register_path(
                deployment=LOCAL_LOCATION,
                location=LOCAL_LOCATION,
                path=path,
                relpath=os.path.basename(path))
        # Build workflow graph
        self._recursive_translate(
            workflow=workflow,
            cwl_element=self.cwl_definition,
            context=context,
            name_prefix=posixpath.sep,
            cwl_name_prefix=cwl_root_prefix)
        # Inject initial inputs
        self._inject_inputs(workflow)
        # Extract requirements
        for hint in (self.cwl_definition.hints or []):
            if not isinstance(hint, MutableMapping):
                context['hints'][hint.class_] = hint
        for requirement in (self.cwl_definition.requirements or []):
            context['requirements'][requirement.class_] = requirement
        requirements = {**context['hints'], **context['requirements']}
        # Extract workflow outputs
        cwl_elements = {_get_name('/', cwl_root_prefix, element.id): element
                        for element in (self.cwl_definition.outputs or [])}
        for output_name, output_value in self.output_ports.items():
            if output_name.lstrip(posixpath.sep).count(posixpath.sep) == 0:
                if port := _percolate_port(output_name, self.output_ports):
                    port_name = output_name.lstrip(posixpath.sep)
                    # Retrieve a local DeployStep
                    target = LocalTarget()
                    deploy_step = self._get_deploy_step(target, workflow)
                    # Create a transformer to enforce deep listing in folders
                    expression_lib, full_js = _process_javascript_requirement(requirements)
                    # Search for dependencies in format expression
                    format_deps = resolve_dependencies(
                        expression=cwl_elements[output_name].format,
                        full_js=full_js,
                        expression_lib=expression_lib)
                    # Build transformer step
                    transformer_step = workflow.create_step(
                        cls=CWLTokenTransformer,
                        name=posixpath.join(port_name + "-collector-transformer"),
                        port_name=port_name,
                        processor=_create_token_processor(
                            port_name=port_name,
                            workflow=workflow,
                            port_type=cwl_elements[output_name].type,
                            cwl_element=cwl_elements[output_name],
                            cwl_name_prefix=posixpath.join(cwl_root_prefix, port_name),
                            schema_def_types=_get_schema_def_types(requirements),
                            format_graph=cwl_elements[output_name].loadingOptions.graph,
                            context=context,
                            check_type=False,
                            default_required_sf=False,
                            force_deep_listing=True))
                    # Add port as transformer input
                    transformer_step.add_input_port(port_name, port)
                    # If there are format dependencies, search for inputs in the port's input steps
                    for dep in format_deps:
                        port_found = False
                        for step in port.get_input_steps():
                            if dep in step.input_ports:
                                transformer_step.add_input_port(dep, step.get_input_port(dep))
                                port_found = True
                                break
                        if not port_found:
                            raise WorkflowDefinitionException("Cannot retrieve {} input port.".format(dep))
                    # Create an output port for the transformer
                    transformer_step.add_output_port(port_name, workflow.create_port())
                    # Create a schedule step and connect it to the local DeployStep
                    schedule_step = workflow.create_step(
                        cls=ScheduleStep,
                        name=posixpath.join(port_name + "-collector", "__schedule__"),
                        connector_port=deploy_step.get_output_port(),
                        target=target)
                    # Add the port as an input of the schedule step
                    schedule_step.add_input_port(port_name, transformer_step.get_output_port())
                    # Add TransferStep to transfer the output in the output_dir
                    transfer_step = workflow.create_step(
                        cls=CWLTransferStep,
                        name=port_name + "-collector",
                        job_port=schedule_step.get_output_port(),
                        writable=True)
                    transfer_step.add_input_port(port_name, transformer_step.get_output_port())
                    transfer_step.add_output_port(port_name, workflow.create_port())
                    # Add the output port of the TransferStep to the workflow output ports
                    workflow.output_ports[port_name] = transfer_step.get_output_port().name
        # Return the final workflow object
        return workflow


class LinkMergeMethod(Enum):
    merge_nested = 1
    merge_flattened = 2


class ScatterPredecessor(object):
    __slots__ = ('step', 'scatter_step')

    def __init__(self,
                 step: str,
                 scatter_step: str):
        self.step: str = step
        self.scatter_step: str = scatter_step
