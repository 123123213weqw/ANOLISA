/**
 * @license
 * Copyright 2025 Qwen Code
 * SPDX-License-Identifier: Apache-2.0
 */

import * as net from 'node:net';
import { afterAll, describe, expect, it } from 'vitest';
import { LspConnectionFactory } from './LspConnectionFactory.js';

/**
 * Builds a one-shot fake language server script that replies to the first
 * request (id 1) with the given result object. The reply body is written with
 * a correct byte-counted Content-Length header so the fixture exercises the
 * client-side framing exactly like a real server would.
 */
function buildReplyServerScript(result: unknown): string {
  const body = JSON.stringify({ jsonrpc: '2.0', id: 1, result });
  return [
    `const body = ${JSON.stringify(body)};`,
    `process.stdout.write('Content-Length: ' + Buffer.byteLength(body) + '\\r\\n\\r\\n' + body);`,
    // Keep the process alive until the client closes stdin so the response
    // cannot be lost to an early exit, then exit to avoid leaking workers.
    'process.stdin.resume();',
    'process.stdin.on("end", () => process.exit(0));',
  ].join('\n');
}

describe('LspConnectionFactory stdio framing', () => {
  it('parses responses whose body contains multibyte UTF-8', async () => {
    const { connection } = await LspConnectionFactory.createStdioConnection(
      process.execPath,
      ['-e', buildReplyServerScript({ greeting: '你好，世界' })],
      {},
      10_000,
    );

    try {
      const response = await Promise.race([
        connection.initialize({}) as Promise<unknown>,
        new Promise<never>((_, reject) =>
          setTimeout(
            () =>
              reject(
                new Error(
                  'response with multibyte UTF-8 body was never delivered to the pending request',
                ),
              ),
            5_000,
          ),
        ),
      ]);

      expect(response).toEqual({ greeting: '你好，世界' });
    } finally {
      connection.end();
    }
  }, 20_000);

  it('parses responses with an ASCII-only body', async () => {
    const { connection } = await LspConnectionFactory.createStdioConnection(
      process.execPath,
      ['-e', buildReplyServerScript({ greeting: 'hello world' })],
      {},
      10_000,
    );

    try {
      const response = await Promise.race([
        connection.initialize({}) as Promise<unknown>,
        new Promise<never>((_, reject) =>
          setTimeout(
            () => reject(new Error('ASCII response was never delivered')),
            5_000,
          ),
        ),
      ]);

      expect(response).toEqual({ greeting: 'hello world' });
    } finally {
      connection.end();
    }
  }, 20_000);
});

describe('LspConnectionFactory buffer retention', () => {
  const openServers: net.Server[] = [];
  const openSockets: net.Socket[] = [];

  afterAll(async () => {
    for (const socket of openSockets) {
      socket.destroy();
    }
    await Promise.all(
      openServers.map(
        (s) => new Promise<void>((resolve) => s.close(() => resolve())),
      ),
    );
  });

  /**
   * Spins up an in-process TCP server that replies to the first bytes it
   * receives with the given raw payload, and connects the LSP client to it.
   */
  async function connectToReplyServer(
    payload: () => Buffer,
  ): Promise<
    Awaited<ReturnType<typeof LspConnectionFactory.createSocketConnection>>
  > {
    const server = net.createServer((socket) => {
      openSockets.push(socket);
      socket.once('data', () => {
        socket.write(payload());
      });
    });
    openServers.push(server);
    await new Promise<void>((resolve) =>
      server.listen(0, '127.0.0.1', resolve),
    );
    const address = server.address();
    if (typeof address === 'string' || !address) {
      throw new Error('failed to listen on an ephemeral port');
    }
    return LspConnectionFactory.createSocketConnection(
      { port: address.port },
      10_000,
    );
  }

  /**
   * The frame parser keeps its unconsumed remainder in a private buffer
   * field. When a message is consumed exactly to the end of the received
   * data, that field must not stay as a zero-length view into the (large)
   * backing store of the received frame: such a view keeps the whole
   * backing ArrayBuffer unreachable for GC for as long as the connection
   * lives, so an idle connection would pin e.g. a 1 MiB
   * semantic-tokens/workspace-symbol response forever.
   */
  function retainedBuffer(
    connection: Awaited<
      ReturnType<typeof LspConnectionFactory.createSocketConnection>
    >['connection'],
  ): Buffer {
    return (connection as unknown as { buffer: Buffer }).buffer;
  }

  it('releases the backing store after a large frame is fully consumed', async () => {
    const result = { payload: 'x'.repeat(1024 * 1024) };
    const body = JSON.stringify({ jsonrpc: '2.0', id: 1, result });
    const { connection } = await connectToReplyServer(() =>
      Buffer.concat([
        Buffer.from(`Content-Length: ${Buffer.byteLength(body)}\r\n\r\n`),
        Buffer.from(body),
      ]),
    );

    try {
      const response = (await Promise.race([
        connection.initialize({}),
        new Promise<never>((_, reject) =>
          setTimeout(
            () => reject(new Error('large response was never delivered')),
            5_000,
          ),
        ),
      ])) as { payload: string };

      expect(response.payload).toHaveLength(1024 * 1024);

      // The frame exactly filled the receive buffer, so the connection is
      // now idle while still holding the parser state. It must not keep a
      // zero-length subarray view that pins the ~1 MiB backing store.
      const leftover = retainedBuffer(connection);
      expect(leftover.length).toBe(0);
      expect(leftover.buffer.byteLength).toBeLessThan(1024);
    } finally {
      connection.end();
    }
  }, 20_000);

  it('releases the backing store after skipping a headerless block', async () => {
    // A large block with no Content-Length header is skipped in one step;
    // when it ends exactly at the \r\n\r\n boundary the same retention
    // rule applies on that code path too. The junk is followed by a valid
    // (multibyte) notification so the test can await the production parse
    // path instead of a fixed delay: a bare sleep could pass while the
    // payload had not even arrived, with the initial empty buffer
    // satisfying both assertions without the skip ever running.
    const junk = Buffer.concat([
      Buffer.from('J'.repeat(64 * 1024)),
      Buffer.from('\r\n\r\n'),
    ]);
    const notificationBody = JSON.stringify({
      jsonrpc: '2.0',
      method: 'test/after-junk',
      params: { greeting: '你好，世界' },
    });
    const framedNotification = Buffer.concat([
      Buffer.from(
        `Content-Length: ${Buffer.byteLength(notificationBody)}\r\n\r\n`,
      ),
      Buffer.from(notificationBody),
    ]);
    const { connection } = await connectToReplyServer(() =>
      Buffer.concat([junk, framedNotification]),
    );

    const parsed = new Promise<void>((resolve, reject) => {
      const timer = setTimeout(
        () =>
          reject(
            new Error(
              'notification following the headerless block was never parsed',
            ),
          ),
        5_000,
      );
      connection.onNotification((message) => {
        if ((message as { method?: string }).method === 'test/after-junk') {
          clearTimeout(timer);
          resolve();
        }
      });
    });

    try {
      // Send a notification (no id, so no pending request to time out) to
      // make the server emit the junk block plus the trailing frame, then
      // await the parsed notification: deterministic proof that the
      // headerless skip and the subsequent byte-length framing both ran
      // before the backing store is inspected.
      connection.send({ jsonrpc: '2.0', method: 'test/ping' });
      await parsed;

      const leftover = retainedBuffer(connection);
      expect(leftover.length).toBe(0);
      expect(leftover.buffer.byteLength).toBeLessThan(1024);
    } finally {
      connection.end();
    }
  }, 20_000);
});
