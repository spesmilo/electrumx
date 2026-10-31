# Copyright (c) 2026, the ElectrumX authors

"""Zcash mempool fee accounting for transactions with shielded bundles."""

from dataclasses import dataclass

from electrumx.server.mempool import MemPool, MemPoolTx


@dataclass(slots=True)
class ZcashMemPoolTx(MemPoolTx):
    # Net public value balance of shielded bundles, in zatoshis.
    fee_adjustment: int = 0


class ZcashMemPool(MemPool):
    """Preserve shielded value balances when calculating Zcash mempool fees."""

    def _make_mempool_tx(self, tx, tx_size) -> ZcashMemPoolTx:
        mempool_tx = super()._make_mempool_tx(tx, tx_size)
        return ZcashMemPoolTx(
            prevouts=mempool_tx.prevouts,
            in_pairs=mempool_tx.in_pairs,
            out_pairs=mempool_tx.out_pairs,
            fee=mempool_tx.fee,
            size=mempool_tx.size,
            fee_adjustment=getattr(tx, 'fee_adjustment', 0),
        )

    def _calculate_fee(self, txid_rev, tx) -> int:
        # Apply shielded balances before clamping the transparent balance.
        fee = (sum(v for _, v in tx.in_pairs) -
               sum(v for _, v in tx.out_pairs) + tx.fee_adjustment)
        if fee < 0 and tx.fee_adjustment:
            raise ValueError(
                f'negative shielded-aware mempool fee for {txid_rev.hex()}')
        return max(0, fee)
