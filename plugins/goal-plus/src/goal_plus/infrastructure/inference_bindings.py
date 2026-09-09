"""Trusted finite role capabilities to Pi tool/schema/extension bindings."""

import json
from pathlib import Path
from typing import Any

import jsonref
from goal_plus.execution.inference_output import strict_output_schema
from goal_plus.ports.inference import InferenceArtifactView, InferenceRoleCapabilities, PiInferenceTools

PI_ANNOTATION_TOOL_NAME = "submit_evidence_annotation"
PI_ARTIFACT_VIEW_LIST_TOOL_NAME = "artifact_view_list"
PI_ARTIFACT_VIEW_READ_TOOL_NAME = "artifact_view_read"
PI_ANNOTATION_OUTPUT_ENV = "GOAL_PLUS_PI_ANNOTATION_OUTPUT"
PI_SCHEDULER_TOOL_NAME = "submit_scheduler_output"
PI_SCHEDULER_OUTPUT_ENV = "GOAL_PLUS_PI_SCHEDULER_OUTPUT"


def annotation_role_capabilities(artifact_views: list[dict[str, Any]]) -> InferenceRoleCapabilities:
    views = tuple(
        InferenceArtifactView(str(view["view_id"]), Path(access["root"]))
        for view in artifact_views
        if isinstance(view, dict) and view.get("view_id")
        and isinstance((access := view.get("access")), dict)
        and access.get("kind") == "filesystem_path" and access.get("root")
    )
    return InferenceRoleCapabilities("annotation", views)


def build_pi_role_tools(
    capabilities: InferenceRoleCapabilities, *, output_schema: dict[str, Any],
) -> PiInferenceTools:
    if capabilities.role == "annotation":
        tool, output_env = PI_ANNOTATION_TOOL_NAME, PI_ANNOTATION_OUTPUT_ENV
        label = "Submit Evidence Annotation"
        description = "Submit the final evidence annotation using the required schema."
    elif capabilities.role == "scheduler":
        if capabilities.artifact_views:
            raise ValueError("scheduler cannot acquire Annotation Artifact Views")
        tool, output_env = PI_SCHEDULER_TOOL_NAME, PI_SCHEDULER_OUTPUT_ENV
        label = "Submit Scheduler Output"
        description = "Submit the scheduler reward and allocation output using the required schema."
    else:
        raise ValueError(f"unsupported inference role: {capabilities.role}")
    views = {view.view_id: str(view.root) for view in capabilities.artifact_views}
    if len(views) != len(capabilities.artifact_views):
        raise ValueError("duplicate Artifact View binding")
    # Pi providers need concrete nested tool types; ref-only fields can be
    # emitted as JSON strings even though Pi validates them as objects.
    parameters = jsonref.replace_refs(
        strict_output_schema(output_schema), proxies=False, lazy_load=False,
        merge_props=True,
    )
    parameters.pop("$defs", None)
    schema = json.dumps(parameters, ensure_ascii=False, separators=(",", ":"))
    prelude = (
        _ARTIFACT_PRELUDE.replace("__ARTIFACT_VIEWS__", json.dumps(views, ensure_ascii=False))
        if views else 'import { writeFileSync } from "node:fs";\n'
    )
    extension = prelude + f"\nconst parameters = {schema};\n\nexport default function (pi: any) {{\n"
    if views:
        extension += _ARTIFACT_TOOLS.replace("__LIST_TOOL__", PI_ARTIFACT_VIEW_LIST_TOOL_NAME).replace("__READ_TOOL__", PI_ARTIFACT_VIEW_READ_TOOL_NAME)
    extension += f'''  pi.registerTool({{
    name: {json.dumps(tool)},
    label: {json.dumps(label)},
    description: {json.dumps(description)},
    parameters,
    async execute(_toolCallId: string, params: unknown) {{
      const outputPath = process.env.{output_env};
      if (!outputPath) throw new Error("missing structured output path");
      writeFileSync(outputPath, JSON.stringify(params), "utf8");
      return {{
        content: [{{ type: "text", text: "Structured output recorded." }}],
        details: {{}},
        terminate: true,
      }};
    }},
  }});
}}
'''
    names = (tool, PI_ARTIFACT_VIEW_LIST_TOOL_NAME, PI_ARTIFACT_VIEW_READ_TOOL_NAME) if views else (tool,)
    return PiInferenceTools(names, extension, output_env)


_ARTIFACT_PRELUDE = '''import { closeSync, lstatSync, openSync, readSync, readdirSync, realpathSync, writeFileSync } from "node:fs";
import { isAbsolute, relative, resolve } from "node:path";

const artifactViews: Record<string, string> = __ARTIFACT_VIEWS__;

function contained(viewId: string, inputPath: string): string {
  const configuredRoot = artifactViews[viewId];
  if (!configuredRoot) throw new Error("unknown or unavailable Artifact View");
  if (isAbsolute(inputPath)) throw new Error("Artifact View paths must be relative");
  const root = realpathSync(configuredRoot);
  const target = realpathSync(resolve(root, inputPath || "."));
  const rel = relative(root, target);
  if (rel === ".." || rel.startsWith("../") || isAbsolute(rel)) {
    throw new Error("Artifact View path escapes its immutable root");
  }
  return target;
}

'''

_ARTIFACT_TOOLS = '''  pi.registerTool({
    name: "__LIST_TOOL__",
    label: "List Artifact View",
    description: "List one directory in an authorized immutable Artifact View.",
    parameters: {
      type: "object",
      properties: {
        view_id: { type: "string", minLength: 1 },
        path: { type: "string", default: "." },
      },
      required: ["view_id", "path"],
      additionalProperties: false,
    },
    async execute(_toolCallId: string, params: any) {
      const target = contained(params.view_id, params.path);
      const entries = readdirSync(target).slice(0, 500).map((name) => {
        const item = lstatSync(resolve(target, name));
        return { name, kind: item.isDirectory() ? "directory" : item.isFile() ? "file" : item.isSymbolicLink() ? "symlink" : "other" };
      });
      return { content: [{ type: "text", text: JSON.stringify(entries) }], details: {} };
    },
  });
  pi.registerTool({
    name: "__READ_TOOL__",
    label: "Read Artifact View",
    description: "Read a bounded byte range from an authorized immutable Artifact View.",
    parameters: {
      type: "object",
      properties: {
        view_id: { type: "string", minLength: 1 },
        path: { type: "string", minLength: 1 },
        offset: { type: "integer", minimum: 0, default: 0 },
        length: { type: "integer", minimum: 1, maximum: 65536, default: 65536 },
      },
      required: ["view_id", "path", "offset", "length"],
      additionalProperties: false,
    },
    async execute(_toolCallId: string, params: any) {
      const target = contained(params.view_id, params.path);
      const handle = openSync(target, "r");
      try {
        const buffer = Buffer.alloc(params.length);
        const count = readSync(handle, buffer, 0, params.length, params.offset);
        return { content: [{ type: "text", text: buffer.subarray(0, count).toString("utf8") }], details: { bytes: count } };
      } finally {
        closeSync(handle);
      }
    },
  });
'''
