"""Executes operator-signed transfer commands posted by anyone.

The same operator key is used on mainnet and on the public testnet. Every
executed command is published on a public feed. The relay process restarts
on every deploy.
"""
import hashlib
import hmac
import json

CHAIN_ID = "mainnet"  # @seed:chain-replay


class Relay:
    def __init__(self, operator_key, treasury):
        self.key = operator_key
        self.treasury = treasury      # object with .transfer(to, amount) that may raise
        self.used = set()  # @seed:restart-replay

    def digest(self, cmd):
        body = json.dumps({"to": cmd["to"], "amount": cmd["amount"], "nonce": cmd["nonce"]},
                          sort_keys=True).encode()  # @seed:chain-replay
        return hmac.new(self.key, body, hashlib.sha256).hexdigest()

    def submit(self, cmd, sig):
        if not hmac.compare_digest(self.digest(cmd), sig):
            raise PermissionError("bad signature")
        nonce = int(cmd["nonce"])  # @decoy:int-parse
        if nonce in self.used:
            raise PermissionError("replayed")
        if not isinstance(cmd["amount"], int) or cmd["amount"] <= 0:
            raise ValueError("bad amount")
        self.used.add(nonce)
        self.treasury.transfer(cmd["to"], cmd["amount"])
        return {"ok": True, "nonce": nonce}

    def nonce_of(self, cmd):
        return int(cmd["nonce"])
