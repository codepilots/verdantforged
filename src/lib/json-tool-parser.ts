/**
 * json-tool-parser.ts — pure-function JSON tool-call parser for WebLLM
 *
 * WebLLM 0.2.78's `chatCompletion` method has a non-standard, broken
 * tool-call parser: it does bare `JSON.parse(modelOutput)` and throws
 * on any non-JSON model reply (see webllm-engine.ts pitfall notes).
 * The workaround used by jakobhoeg/browser-ai (Apache 2.0) is to
 * never let WebLLM's parser see the model output: pass the tool
 * schemas as part of the system prompt (so the model knows the
 * available tools), call `engine.chat.completions.create()` WITHOUT
 * a `tools` field, and parse the model's plain text response
 * ourselves.
 *
 * This file is adapted from
 *   https://github.com/jakobhoeg/browser-ai/blob/main/packages/vercel/shared/src/tool-calling/parse-json-function-calls.ts
 *   https://github.com/jakobhoeg/browser-ai/blob/main/packages/vercel/shared/src/tool-calling/build-json-system-prompt.ts
 * under the Apache License 2.0. Copyright 2025 Jakob Hoeg Mørk. The
 * functions are pure, dependency-free, and can be safely vendored.
 *
 * The parser handles 5 distinct tool-call output formats that
 * different model families emit:
 *
 *   1. Markdown fence:
 *        ```tool_call
 *        {"name": "tool", "arguments": {...}}
 *        ```
 *
 *   2. XML tags (Hermes-3, OpenAI's tools spec):
 *        <tool_call>
 *        {"name": "tool", "arguments": {...}}
 *        </tool_call>
 *
 *   3. Python-style:
 *        [functionName(arg="value", arg2="value2")]
 *
 *   4. Llama-style delimited (Gemma4's <|tool_call|>):
 *        <|tool_call|> call:name{key:value,...} <|/tool_call|>
 *
 *   5. Raw JSON (single object, array, or newline-separated)
 *
 * The system-prompt builder formats the tool schemas as JSON inside
 * the system prompt, with explicit instructions on the markdown-fence
 * format the model should emit. The model is told: "If no tool is
 * needed, respond directly without tool_call fences" — so plain
 * greetings still work.
 *
 * Usage in portable-hermes.ts::chat():
 *
 *   const tools = JSON.parse(String(toolsJson));   // OpenAI-shape tool defs
 *   const sysPrompt = buildJsonToolSystemPrompt(PORTABLE_SYSTEM_PROMPT, tools);
 *   const reply = await engine.chat.completions.create({
 *     messages: [{role: 'system', content: sysPrompt}, ...history],
 *     // NOTE: no `tools` field — see header comment
 *     temperature: 0.7,
 *     max_tokens: 256,
 *   });
 *   const text = reply.choices[0].message.content ?? '';
 *   const { toolCalls: parsed, textContent } = parseJsonFunctionCalls(text);
 *   // parsed is `[{ toolCallId, toolName, args }]` ready for skill execution
 */

// ─── types ─────────────────────────────────────────────────────────

/** JSON Schema 7 (we use a minimal inline definition to avoid a dep). */
export type JSONSchema = {
  type?: 'object' | 'array' | 'string' | 'number' | 'integer' | 'boolean' | 'null';
  properties?: Record<string, JSONSchema>;
  required?: string[];
  items?: JSONSchema;
  description?: string;
  enum?: unknown[];
  [k: string]: unknown;
};

/** OpenAI-shape tool definition. */
export type ToolDefinition = {
  name: string;
  description?: string;
  parameters: JSONSchema;
};

/** A single tool call extracted from the model response. */
export interface ParsedToolCall {
  type: 'tool-call';
  toolCallId: string;
  toolName: string;
  args: Record<string, unknown>;
}

/** Result of parsing a response that may contain tool calls. */
export interface ParsedResponse {
  toolCalls: ParsedToolCall[];
  /** The response text with the tool-call blocks removed. */
  textContent: string;
}

// ─── parser config ─────────────────────────────────────────────────

export interface ParseJsonFunctionCallsOptions {
  /** Support XML-style tags: <tool_call>...</tool_call> */
  supportXmlTags?: boolean;
  /** Support Python-style: [functionName(arg="value")] */
  supportPythonStyle?: boolean;
  /** Support "parameters" as alias for "arguments" (Llama format) */
  supportParametersField?: boolean;
  /** Support call:name{key:value} style delimited with <|tool_call|>...<|/tool_call|> */
  supportCallColonStyle?: boolean;
}

const DEFAULT_OPTIONS: ParseJsonFunctionCallsOptions = {
  supportXmlTags: true,
  supportPythonStyle: true,
  supportParametersField: true,
  supportCallColonStyle: true,
};

function generateToolCallId(): string {
  return `call_${Date.now()}_${Math.random().toString(36).slice(2, 9)}`;
}

/**
 * Parses key:value parameter pairs from the call:name{key:value,...} format.
 * Values are coerced to numbers/booleans/null when possible.
 */
function parseCallColonParams(params: string): Record<string, unknown> {
  const args: Record<string, unknown> = {};
  if (!params || !params.trim()) return args;
  const pairs = params.split(',').map((s) => s.trim());
  for (const pair of pairs) {
    const colonIndex = pair.indexOf(':');
    if (colonIndex > 0) {
      const key = pair.substring(0, colonIndex).trim();
      const rawValue = pair.substring(colonIndex + 1).trim();
      if (rawValue === 'true') {
        args[key] = true;
      } else if (rawValue === 'false') {
        args[key] = false;
      } else if (rawValue === 'null') {
        args[key] = null;
      } else {
        const numValue = Number(rawValue);
        args[key] = !isNaN(numValue) && rawValue !== '' ? numValue : rawValue;
      }
    }
  }
  return args;
}

function buildRegex(options: ParseJsonFunctionCallsOptions): RegExp {
  const patterns: string[] = [];
  // Always support markdown fences (our preferred format).
  patterns.push('```tool[_\\-]?call\\s*([\\s\\S]*?)```');
  if (options.supportXmlTags) {
    patterns.push('<tool_call>\\s*([\\s\\S]*?)\\s*</tool_call>');
  }
  if (options.supportPythonStyle) {
    patterns.push('\\[(\\w+)\\(([^)]*)\\)\\]');
  }
  if (options.supportCallColonStyle) {
    patterns.push('<\\|tool_call>\\s*([\\s\\S]*?)\\s*<\\|/tool_call\\|>');
  }
  return new RegExp(patterns.join('|'), 'gi');
}

// ─── main parser ───────────────────────────────────────────────────

/**
 * Parses JSON-formatted tool calls from a model's response. Supports
 * multiple formats (see header). Returns the structured tool calls
 * and the text with the call blocks removed.
 */
export function parseJsonFunctionCalls(
  response: string,
  options: ParseJsonFunctionCallsOptions = DEFAULT_OPTIONS,
): ParsedResponse {
  const mergedOptions = { ...DEFAULT_OPTIONS, ...options };
  const regex = buildRegex(mergedOptions);
  const matches = Array.from(response.matchAll(regex));
  regex.lastIndex = 0;

  if (matches.length === 0) {
    return { toolCalls: [], textContent: response };
  }

  const toolCalls: ParsedToolCall[] = [];
  let textContent = response;

  for (const match of matches) {
    const fullMatch = match[0];
    textContent = textContent.replace(fullMatch, '');

    try {
      // Python-style: [functionName(args)]
      if (mergedOptions.supportPythonStyle && match[0].startsWith('[')) {
        const pythonMatch = /\[(\w+)\(([^)]*)\)\]/.exec(match[0]);
        if (pythonMatch) {
          const [, funcName, pythonArgs] = pythonMatch;
          const args: Record<string, unknown> = {};
          if (pythonArgs && pythonArgs.trim()) {
            const argPairs = pythonArgs.split(',').map((s) => s.trim());
            for (const pair of argPairs) {
              const equalIndex = pair.indexOf('=');
              if (equalIndex > 0) {
                const key = pair.substring(0, equalIndex).trim();
                let value = pair.substring(equalIndex + 1).trim();
                if (
                  (value.startsWith('"') && value.endsWith('"')) ||
                  (value.startsWith("'") && value.endsWith("'"))
                ) {
                  value = value.substring(1, value.length - 1);
                }
                args[key] = value;
              }
            }
          }
          toolCalls.push({
            type: 'tool-call',
            toolCallId: generateToolCallId(),
            toolName: funcName,
            args,
          });
          continue;
        }
      }

      // call:name{params} style (Gemma4 inside <|tool_call|>)
      if (mergedOptions.supportCallColonStyle) {
        const callMatch = fullMatch.match(/call:(\w+)\{([^}]*)\}/);
        if (callMatch) {
          const [, funcName, params] = callMatch;
          toolCalls.push({
            type: 'tool-call',
            toolCallId: generateToolCallId(),
            toolName: funcName,
            args: parseCallColonParams(params),
          });
          continue;
        }
      }

      // JSON inside the matched fence (markdown, XML, etc.)
      const innerContent = match.slice(1).find((g) => g !== undefined) || '';
      const trimmed = innerContent.trim();
      if (!trimmed) continue;

      try {
        const parsed = JSON.parse(trimmed);
        const callsArray = Array.isArray(parsed) ? parsed : [parsed];
        for (const call of callsArray) {
          if (!call?.name) continue;
          let args =
            call.arguments ||
            (mergedOptions.supportParametersField ? call.parameters : null) ||
            {};
          if (typeof args === 'string') {
            try { args = JSON.parse(args); } catch { /* keep as string */ }
          }
          toolCalls.push({
            type: 'tool-call',
            toolCallId: call.id || generateToolCallId(),
            toolName: call.name,
            args: args as Record<string, unknown>,
          });
        }
      } catch {
        // Single JSON parse failed — try newline-separated objects.
        const lines = trimmed.split('\n').filter((l) => l.trim());
        for (const line of lines) {
          try {
            const call = JSON.parse(line.trim());
            if (!call?.name) continue;
            let args =
              call.arguments ||
              (mergedOptions.supportParametersField ? call.parameters : null) ||
              {};
            if (typeof args === 'string') {
              try { args = JSON.parse(args); } catch { /* keep as string */ }
            }
            toolCalls.push({
              type: 'tool-call',
              toolCallId: call.id || generateToolCallId(),
              toolName: call.name,
              args: args as Record<string, unknown>,
            });
          } catch {
            // skip invalid JSON line
          }
        }
      }
    } catch (err) {
      console.warn('[json-tool-parser] failed to parse tool call:', err);
    }
  }

  textContent = textContent.replace(/\n{2,}/g, '\n');
  return { toolCalls, textContent: textContent.trim() };
}

// ─── system prompt builder ─────────────────────────────────────────

/**
 * Build a system prompt that tells the model which tools are
 * available and instructs it to emit tool calls inside a
 * ```tool_call``` markdown fence. This replaces the OpenAI `tools`
 * parameter (which WebLLM 0.2.78 cannot parse the output of).
 */
export function buildJsonToolSystemPrompt(
  originalSystemPrompt: string | undefined,
  tools: ToolDefinition[],
  options?: { allowParallelToolCalls?: boolean },
): string {
  if (!tools || tools.length === 0) {
    return originalSystemPrompt || '';
  }
  const parallelInstruction = options?.allowParallelToolCalls
    ? 'You may request multiple independent tool calls in the same response.'
    : 'Only request one tool call at a time. Wait for tool results before asking for another tool.';

  const toolSchemas = tools.map((tool) => ({
    name: tool.name,
    description: tool.description ?? 'No description provided.',
    parameters: tool.parameters || { type: 'object', properties: {} },
  }));
  const toolsJson = JSON.stringify(toolSchemas, null, 2);

  const instructionBody = `You are a helpful AI assistant with access to tools.

# Available Tools
${toolsJson}

# Tool Calling Instructions
${parallelInstruction}

To call a tool, output JSON in this exact format inside a \`\`\`tool_call code fence:

\`\`\`tool_call
{"name": "tool_name", "arguments": {"param1": "value1", "param2": "value2"}}
\`\`\`

Tool responses will be provided in \`\`\`tool_result fences. Each line contains JSON like:
\`\`\`tool_result
{"id": "call_123", "name": "tool_name", "result": {...}, "error": false}
\`\`\`
Use the \`result\` payload (and treat \`error\` as a boolean flag) when continuing the conversation.

Important:
- Use exact tool and parameter names from the schema above
- Arguments must be a valid JSON object matching the tool's parameters
- You can include brief reasoning before or after the tool call
- If no tool is needed, respond directly without tool_call fences`;

  if (originalSystemPrompt?.trim()) {
    return `${originalSystemPrompt.trim()}\n\n${instructionBody}`;
  }
  return instructionBody;
}
