/**
 * ferry × flare 接入示例
 *
 * 用法（在 flare 里）：
 *   import { ferryTool, ferryTranslate } from "./examples/flare-tool.js";
 *   // 注册到工具表
 *   tools.push(ferryTool);
 *   // 或直接调用
 *   const r = await ferryTranslate("ls -la", "windows");
 *   if (r.status === "equivalent") await exec(r.command);
 *
 * 设计要点：
 * - 用 ferry 的退出码判断等价级别，而不是自己解析文本
 * - 永远把 caveats 交给模型/用户看，不要吞掉
 * - 不要 fallback 到"让模型自己猜"——查不到就按 unsupported 处理
 */

import { execFile } from "node:child_process";

export type Platform = "linux" | "macos" | "windows";
export type Status = "equivalent" | "approximate" | "unsupported" | "error";

export interface FerrySegment {
  input: string;
  operator: string;
  command: string | null;
  status: Status;
  kind: string;
  caveats: string[];
}

export interface FerryResult {
  schema: number;
  input: string;
  source: Platform;
  target: Platform;
  shell: string;
  status: Status;
  command: string | null;
  confidence: "high" | "medium" | "low" | "none";
  inferred: boolean;
  llm_used: boolean;
  caveats: string[];
  notes: string[];
  segments: FerrySegment[];
}

export interface FerryOptions {
  /** 目标 shell，Windows 默认 powershell */
  shell?: "bash" | "zsh" | "powershell" | "cmd";
  /** 源平台，缺省由 ferry 按命令名推断 */
  from?: Platform;
  /** Windows 盘符在 POSIX 下的写法 */
  pathStyle?: "wsl" | "msys";
  /** 查不到时用 LLM 兜底（需 API key；结果标记 inferred） */
  llm?: boolean;
  timeoutMs?: number;
}

const EXIT_TO_STATUS: Record<number, Status> = { 0: "equivalent", 2: "approximate", 3: "unsupported" };

/** 调用 ferry，返回结构化结果。命令本身不会被 ferry 执行。 */
export function ferryTranslate(
  command: string,
  target: Platform,
  opts: FerryOptions = {},
): Promise<FerryResult> {
  const bin = process.env.FERRY_BIN ?? "ferry";
  const args = ["translate", command, "--to", target, "--json"];
  if (opts.from) args.push("--from", opts.from);
  if (opts.shell) args.push("--shell", opts.shell);
  if (opts.pathStyle) args.push("--path-style", opts.pathStyle);
  if (opts.llm) args.push("--llm");

  return new Promise((resolve, reject) => {
    execFile(bin, args, { timeout: opts.timeoutMs ?? 15000, maxBuffer: 1 << 20 }, (err, stdout) => {
      // 退出码 2/3 不是错误，是状态；只有 4 或进程失败才是错误
      const code = (err as NodeJS.ErrnoException & { code?: number })?.code;
      if (err && typeof code !== "number") return reject(err);
      try {
        const parsed = JSON.parse(stdout) as FerryResult;
        parsed.status = EXIT_TO_STATUS[code as number] ?? parsed.status;
        resolve(parsed);
      } catch (e) {
        reject(new Error(`ferry 输出无法解析: ${String(e)}\n${stdout.slice(0, 400)}`));
      }
    });
  });
}

/** 目标平台的速查摘要，塞进 system prompt 用（会话开始时调一次即可）。 */
export function ferryCheatSheet(target: Platform, maxChars = 3000): Promise<string> {
  const bin = process.env.FERRY_BIN ?? "ferry";
  return new Promise((resolve, reject) => {
    execFile(
      bin,
      ["prompt-export", "--to", target, "--max-chars", String(maxChars)],
      { timeout: 10000, maxBuffer: 1 << 20 },
      (err, stdout) => (err ? reject(err) : resolve(stdout)),
    );
  });
}

/**
 * function-calling 工具定义（OpenAI / DeepSeek 兼容）。
 * 描述里刻意写清"什么时候该调用"，这是模型会不会用对的关键。
 */
export const ferryTool = {
  type: "function" as const,
  function: {
    name: "translate_shell_command",
    description:
      "把一条 shell 命令翻译到另一个操作系统。在写跨平台命令（Linux/macOS/Windows）之前调用。" +
      "返回 status: equivalent(可直接执行) / approximate(需核对 caveats) / unsupported(无等价，换方案)。" +
      "注意：Linux→macOS 的真实差异通常不在命令名而在参数（BSD 与 GNU 不同）。",
    parameters: {
      type: "object",
      properties: {
        command: { type: "string", description: "要翻译的命令，可含管道和 &&" },
        target: { type: "string", enum: ["linux", "macos", "windows"] },
        shell: { type: "string", enum: ["bash", "zsh", "powershell", "cmd"], description: "目标 shell" },
      },
      required: ["command", "target"],
    },
  },
};

/** 工具调用分发示例：把工具返回内容压缩成模型能直接读的一段话。 */
export async function handleFerryToolCall(args: { command: string; target: Platform; shell?: FerryOptions["shell"] }): Promise<string> {
  const r = await ferryTranslate(args.command, args.target, { shell: args.shell });
  const head =
    r.status === "equivalent"
      ? `可直接执行：${r.command}`
      : r.status === "approximate"
        ? `近似等价（执行前核对）：${r.command}`
        : `目标平台无等价命令，不要执行。${r.caveats[0] ?? ""}`;
  return [head, ...r.caveats.map((c) => `- ${c}`)].join("\n");
}
