/** Official DSH general tools, with Clear's existing operation journal at the seam. */
import FileSystem from '@deepseek-ai/dsh-fs-sandbox'
import WorkingDirectory from '@deepseek-ai/dsh-working-directory'
import Subprocess from '@deepseek-ai/dsh-subprocess-local'
import Sandbox from '@deepseek-ai/dsh-sandbox-local'
import SandboxPolicy from '@deepseek-ai/dsh-sandbox-policy'
import Approval from '@deepseek-ai/dsh-user-approval'
import Pwsh from '@deepseek-ai/dsh-pwsh-sandbox'
import Jobs from '@deepseek-ai/dsh-jobs-local'
import Web from '@deepseek-ai/dsh-web'
import * as ToolFs from '@deepseek-ai/dsh-tool-fs'
import * as ToolSearch from '@deepseek-ai/dsh-tool-fs-search'
import * as ObservationPolicy from '@deepseek-ai/dsh-fs-observation-policy'
import * as ToolPwsh from '@deepseek-ai/dsh-tool-pwsh'
import * as ToolJobs from '@deepseek-ai/dsh-tool-jobs'
import * as ShellEnv from '@deepseek-ai/dsh-shell-env'
import * as WebFetch from '@deepseek-ai/dsh-web-fetch-http'
import * as ToolWeb from '@deepseek-ai/dsh-tool-web'
import { realpathSync } from 'node:fs'
import Skills from '@deepseek-ai/dsh-skill'
import * as SkillFiles from '@deepseek-ai/dsh-skill-filesystem'
import * as ToolSkill from '@deepseek-ai/dsh-tool-skill'
import Attachments from '@deepseek-ai/dsh-attachment-local'
import * as McpClient from '@deepseek-ai/dsh-mcp-client'
import Subagents from '@deepseek-ai/dsh-subagent'
import * as SpawnSubagent from '@deepseek-ai/dsh-subagent-spawn-in-process'
import * as ToolSubagent from '@deepseek-ai/dsh-tool-subagent'
import * as SubagentControl from '@deepseek-ai/dsh-tool-subagent-control'
import { resolve, relative, isAbsolute } from 'node:path'

const childTools = new Set(['read', 'read_image', 'glob', 'grep', 'write', 'edit', 'pwsh',
  'job_list', 'job_output', 'job_kill', 'skill', 'web_fetch', 'send_message'])

class WorkspacePolicy extends SandboxPolicy {
  resolve(request = {}) {
    // The DATA root can move during cold restore. Historical session headers
    // retain their original cwd; current permissions follow the host-owned
    // task workspace, including for delegated children.
    return { ...super.resolve(request), mode: 'workspace-write', workspaceRoot: this.workspaceRoot }
  }
}

class WorkspacePwsh extends Pwsh {
  argv(spec) {
    const directory = realpathSync(spec.workdir)
    if (!/^f:\\/i.test(directory)) throw new Error('PowerShell working directory must resolve to local F drive')
    // Restricted Windows PowerShell can reset its initial location to PSHOME.
    // Set the real task location explicitly. Its language mode also rejects
    // UTF8Encoding::new(), used by the upstream optional encoding preamble.
    const location = directory.replaceAll("'", "''")
    return [this.pwshPath, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command',
      `Set-Location -LiteralPath '${location}' -ErrorAction Stop; ${spec.command}`]
  }
}

export async function installFoundation(ctx, spec, rpc, flush) {
  const cwd = spec.cwd
  for (const [plugin, config] of [
    [WorkspacePolicy, { mode: 'workspace-write', workspaceRoot: cwd }],
    [Approval, { policy: 'never' }],
    [FileSystem, { cwd }],
    [WorkingDirectory, { defaultDirectory: cwd }],
    [Subprocess, {}], [Skills, {}],
    [SkillFiles, { includeDefaultRoots: false, customSkillDirs: spec.skill_dirs || [], watch: false,
      dshHome: process.env.DSH_HOME, agentsHome: process.env.DSH_HOME + '/agents' }],
    [Attachments, { dshHome: process.env.DSH_HOME }],
    [Sandbox, {}],
    [WorkspacePwsh, { cwd, timeoutMs: 60000, maxTimeoutMs: 300000 }],
    [Jobs, {}], [ShellEnv, { dshHome: process.env.DSH_HOME }],
    [ObservationPolicy, {}], [ToolFs, {}], [ToolSearch, { sampleOverCapGlobResults: false }],
    [ToolPwsh, {}], [ToolJobs, { completionDelivery: 'quiet' }], [ToolSkill, {}],
    [Web, { fetchProvider: 'http' }], [WebFetch, {}],
    [ToolWeb, { search: false, fetch: true }],
  ]) await ctx.plugin(plugin, config).await()

  const integrationStatus = []
  for (const server of spec.integrations?.mcp_servers || []) {
    try {
      let config = { ...server, failOnStartupError: true, reconnect: { enabled: false } }
      if (config.transport === 'stdio') {
        const confined = await ctx.sandbox.confine([config.command, ...(config.args || [])], {
          mode: 'workspace-write', workspaceRoot: cwd, sessionId: spec.session_id,
        })
        config = { ...config, command: confined.argv[0], args: confined.argv.slice(1), cwd,
          env: { ...(config.env || {}) } }
      }
      await ctx.plugin(McpClient, config).await()
      integrationStatus.push({ name: config.serverName, available: true, transport: config.transport })
    } catch (error) {
      integrationStatus.push({ name: server.serverName, available: false,
        reason: 'MCP连接或工具发现失败，请核对本地扩展设置' })
    }
  }
  if (spec.integrations?.subagents !== false) {
    await ctx.plugin(Subagents, { maxDepth: 1, maxActiveSubagents: 3 }).await()
    await ctx.plugin(SpawnSubagent, {}).await()
    await ctx.plugin(ToolSubagent, { provider: 'spawn', modelSelectionSettings: false, maxDepth: 1 }).await()
    await ctx.plugin(SubagentControl, {}).await()
  }

  const names = new Set(ctx.tools.schemas().map(tool => tool.name))
  for (const name of ['subagent', 'send_message', 'interrupt_agent']) names.add(name)
  ctx.tools.guard(exec => {
    if (exec.agent?.session.id !== spec.session_id && !childTools.has(exec.name)) {
      return 'Delegated children use the general task tools; platform writes remain with the parent.'
    }
    if (!names.has(exec.name)) return
    const args = exec.arguments || {}
    if (exec.name === 'subagent' && args.cwd) {
      const directory = realpathSync(resolve(cwd, args.cwd))
      const path = relative(realpathSync(cwd), directory)
      if (path.startsWith('..') || isAbsolute(path)) return 'Child working directory must remain inside the parent task workspace.'
    }
    if (args.sandbox_permissions && args.sandbox_permissions !== 'use_default') {
      return 'This workspace keeps file writes inside its F-drive task directory; escalation is unavailable.'
    }
  })
  ctx.on('tools/execute', async (exec, next) => {
    if (!names.has(exec.name)) return next()
    await flush()
    const identity = { tool: exec.name, arguments: exec.arguments, call_id: exec.callId,
      agent_session_id: exec.agent?.session.id || spec.session_id }
    const admission = await rpc('clear/native-begin', identity, exec.signal)
    if (admission.outcome) return admission.outcome
    const result = await next()
    // The canonical outcome is JSON; Error instances are represented by the
    // pipeline's structured failure identity rather than their private fields.
    const outcome = JSON.parse(JSON.stringify(result))
    await rpc('clear/native-result', { ...identity, outcome }, exec.signal)
    return result
  })
  return { names, childTools, integrationStatus, schemas: agent => ctx.tools.schemas(agent).filter(tool => names.has(tool.name)) }
}
