/**
 * @license
 * Copyright 2025 Qwen Code
 * SPDX-License-Identifier: Apache-2.0
 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Config as CoreConfig } from '../config/config.js';
import type { FileDiscoveryService } from '../services/fileDiscoveryService.js';
import type { WorkspaceContext } from '../utils/workspaceContext.js';
import { LspServerManager } from './LspServerManager.js';
import type { LspServerConfig } from './types.js';

function buildManager(): LspServerManager {
  const config = {
    isTrustedFolder: () => true,
  } as unknown as CoreConfig;
  const workspaceContext = {
    getDirectories: () => [],
  } as unknown as WorkspaceContext;
  const fileDiscoveryService = {
    shouldIgnoreFile: () => false,
  } as unknown as FileDiscoveryService;

  return new LspServerManager(config, workspaceContext, fileDiscoveryService, {
    requireTrustedWorkspace: false,
    workspaceRoot: '/tmp',
  });
}

const serverConfig: LspServerConfig = {
  name: 'broken-server',
  languages: ['plaintext'],
  transport: 'stdio',
  rootUri: 'file:///tmp',
};

describe('LspServerManager startup cleanup', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('disposes the process and connection when initialization fails', async () => {
    const manager = buildManager();
    manager.setServerConfigs([{ ...serverConfig }]);

    const kill = vi.fn();
    const end = vi.fn();
    const send = vi.fn();
    const shutdown = vi.fn(async () => {});

    vi.spyOn(
      LspServerManager.prototype as unknown as {
        createLspConnection: (config: LspServerConfig) => Promise<unknown>;
      },
      'createLspConnection',
    ).mockResolvedValue({
      connection: { send, shutdown, end },
      process: { kill, exitCode: null, killed: false },
      shutdown,
      exit: vi.fn(),
      initialize: async () => {
        throw new Error('LSP request timeout: initialize');
      },
    });

    await manager.startAll();

    expect(manager.getStatus().get('broken-server')).toBe('FAILED');

    // The spawned server process must not outlive the failed startup.
    expect(kill).toHaveBeenCalled();
    // The JSON-RPC connection (and its stdio pipes) must be disposed.
    expect(end).toHaveBeenCalled();
  });
});
