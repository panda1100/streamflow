import argparse
import json
import logging
import os

import cwl_utils.parser
import cwltool.context
import cwltool.load_tool
import cwltool.loghandler
import cwltool.main
import cwltool.utils

from streamflow.config.config import WorkflowConfig
from streamflow.core.context import StreamFlowContext
from streamflow.cwl.translator import CWLTranslator
from streamflow.log_handler import logger
from streamflow.workflow.executor import StreamFlowExecutor


def _parse_arg(path: str, context: StreamFlowContext):
    if '://' in path:
        return [path]
    elif os.path.isabs(path):
        return [os.path.join(context.config_dir, path)]
    else:
        return [path]


def _parse_args(workflow_config: WorkflowConfig, context: StreamFlowContext):
    cwl_config = workflow_config.config
    args = _parse_arg(cwl_config['file'], context)
    if 'settings' in cwl_config:
        args.extend(_parse_arg(cwl_config['settings'], context))
    return args


async def main(workflow_config: WorkflowConfig, context: StreamFlowContext, args: argparse.Namespace):
    # Parse input arguments
    cwl_args = _parse_args(workflow_config, context)
    # Configure log level
    if args.quiet:
        # noinspection PyProtectedMember
        cwltool.loghandler._logger.setLevel(logging.WARN)
    # Load CWL workflow definition
    cwl_definition = cwl_utils.parser.load_document_by_uri(cwl_args[0])
    if len(cwl_args) == 2:
        loading_context = cwltool.context.LoadingContext()
        loader = cwltool.load_tool.default_loader(
            loading_context.fetcher_constructor)
        loader.add_namespaces(cwl_definition.loadingOptions.namespaces or {})
        cwl_inputs, _ = loader.resolve_ref(
            cwl_args[1],
            checklinks=False,
            content_types=cwltool.CWL_CONTENT_TYPES)

        def expand_formats(p) -> None:
            if "format" in p:
                p["format"] = loader.expand_url(p["format"], "")

        cwltool.utils.visit_class(cwl_inputs, ("File",), expand_formats)
    else:
        cwl_inputs = {}
    # Transpile CWL workflow to the StreamFlow representation
    logger.info("Processing workflow {}".format(args.name))
    translator = CWLTranslator(
        context=context,
        name=args.name,
        output_directory=args.outdir,
        cwl_definition=cwl_definition,
        cwl_inputs=cwl_inputs,
        workflow_config=workflow_config)
    logger.info("Building workflow execution plan")
    workflow = translator.translate()
    await workflow.save(context)
    logger.info("Building of workflow execution plan terminated with status COMPLETED")
    executor = StreamFlowExecutor(workflow)
    logger.info("Running workflow {}".format(args.name))
    output_tokens = await executor.run()
    logger.info("Workflow execution terminated with status COMPLETED")
    print(json.dumps(output_tokens, sort_keys=True, indent=4))
