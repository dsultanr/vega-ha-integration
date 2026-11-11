"""Authenticated encrypted cloudru relay over the existing Vega TCP listener.

VHR1 preface, mutual HMAC challenge, direction-specific AES-256-GCM keys.
Counters are implicit and strictly ordered; reconnects use fresh random nonces.
"""
import asyncio
import hashlib
import hmac
import json
import secrets
import struct
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b'VHR1\n'
MAX_MESSAGE = 256 * 1024


class Channel:
    def __init__(self, reader, writer, txkey, rxkey):
        self.reader, self.writer = reader, writer
        self.tx, self.rx = AESGCM(txkey), AESGCM(rxkey)
        self.sent = self.received = 0
        self.lock = asyncio.Lock()

    async def send(self, message):
        raw = json.dumps(message, separators=(',', ':'), allow_nan=False).encode()
        if len(raw) > MAX_MESSAGE:
            raise ValueError('Relay message too large')
        async with self.lock:
            nonce = self.sent.to_bytes(12, 'big')
            encrypted = self.tx.encrypt(nonce, raw, MAGIC)
            self.sent += 1
            self.writer.write(struct.pack('!I', len(encrypted)) + encrypted)
            await asyncio.wait_for(self.writer.drain(), 10)

    async def recv(self):
        length = struct.unpack('!I', await self.reader.readexactly(4))[0]
        if not 16 <= length <= MAX_MESSAGE + 16:
            raise ValueError('Invalid relay record length')
        data = await self.reader.readexactly(length)
        raw = self.rx.decrypt(self.received.to_bytes(12, 'big'), data, MAGIC)
        self.received += 1
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            raise ValueError('Expected relay object')
        return obj

    def close(self):
        self.writer.close()


def digest(key, message):
    return hmac.new(key, message, hashlib.sha256).digest()


async def handshake(reader, writer, token, server=False):
    key = bytes.fromhex(token)
    if len(key) != 32:
        raise ValueError('Relay key must be 32 random bytes')
    if server:
        nonce = secrets.token_bytes(32)
        writer.write(nonce)
        await writer.drain()
        response = await asyncio.wait_for(reader.readexactly(64), 5)
        peer = response[:32]
        transcript = MAGIC + nonce + peer
        if not hmac.compare_digest(response[32:], digest(key, b'client' + transcript)):
            raise ValueError('Relay authentication failed')
        writer.write(digest(key, b'server' + transcript))
        await writer.drain()
    else:
        writer.write(MAGIC)
        await writer.drain()
        nonce = await asyncio.wait_for(reader.readexactly(32), 5)
        peer = secrets.token_bytes(32)
        transcript = MAGIC + nonce + peer
        writer.write(peer + digest(key, b'client' + transcript))
        await writer.drain()
        response = await asyncio.wait_for(reader.readexactly(32), 5)
        if not hmac.compare_digest(response, digest(key, b'server' + transcript)):
            raise ValueError('Relay authentication failed')
    c2s = digest(key, b'c2s' + transcript)
    s2c = digest(key, b's2c' + transcript)
    return Channel(reader, writer, s2c if server else c2s, c2s if server else s2c)
