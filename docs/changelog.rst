===========
 ChangeLog
===========


Version 1.18.0 (14 June 2025)
=============================

* protocol:
   - add basic countermeasures against traffic analysis by padding the jsonrpc payload
     with whitespaces to have ~uniform-size TCP packets, and by artificially delaying
     sending messages a bit. (`spesmilo/electrumx#301`_)
* dependencies:
   - bump required aiorpcx to >=0.25.0,<0.26


Version 1.17.0 (16 Apr 2025)
============================

Long time no see!

* coins:
   - rm support for legacy coin name "BitcoinSegwit" (`ca59151d`_)
     (users should just change their config files to :code:`COIN=Bitcoin`)
   - add support for Bitcoin Signet (`spesmilo/electrumx#122`_)
   - add support for Bitcoin Testnet4 (`spesmilo/electrumx#273`_)
* dependencies:
   - bump required python to >=3.10
   - bump required aiorpcx to >=0.23.0,<0.25
   - rm "pylru" dep, instead bundle stripped-down "cachetools" (`spesmilo/electrumx#248`_)
* session: log IP address of excessive resusage sessions (`spesmilo/electrumx#75`_)
* robustness:
   - session: more robust session handling (`25339328`_, `8198ab99`_)
   - daemon: catch RPC_PARSE_ERROR from bitcoind and simply retry (`31489365`_)
* env:
   - add MAX_RECV environment variable (`a0d3053f`_)
   - increase default for MAX_SEND (`2969ab11`_)
* protocol: increase resolution of :code:`mempool.get_fee_histogram` (`265a5a87`_)
* LocalRPC:
   - add --timeout arg (`5f4dd2cd`_)
   - add commands to help debug memory leaks (`f5582b29`_)
* build/maintenance:
   - add scripts for reproducible build (`0ba87447`_)
   - migrate from setup.py to pyproject.toml
   - refactor file layout flat->src (`spesmilo/electrumx#298`_)
* some internal refactors in preparation for future changes


Version 1.16.0 (10 Dec 2020)
============================

Note: this is the first release since forking from kyuupichan/electrumx.
kyuupichan has the :code:`electrumx` name on PyPI, so we needed a new name there.
We are using the `e-x <https://pypi.org/project/e-x/>`_ name on PyPI, so you can
install this package via e.g. :code:`pip install e-x`.

* security: a vulnerability has been fixed that allowed a remote attacker to
  crash electrumx if peer discovery was enabled (`#22`_)
* fixed some peer-discovery-related bugs (e.g. `#35`_)
* ENV: when using Bitcoin, the COIN ENV var can now be set to :code:`Bitcoin`.
  For compatibility, using :code:`BitcoinSegwit` will also keep working.
  (`#5`_)
* session resource limits: made more useful in general. connection-time-based
  grouping has been removed (`#70`_). Disconnects of over-limit sessions happen
  sooner (`4b3f6510`_). Subnet sizes for IP-based grouping can now be
  configured (`a61136c5`_).
* protocol: :code:`mempool.get_fee_histogram` now returns fee rates with
  0.1 sat/byte resolution instead of 1 sat/byte, and the compact histogram
  is calculated differently. (`#67`_)
* performance: bitcoind is now queried less frequently as estimatefee,
  relayfee, and server.banner requests are now cached (`#24`_)
* performance: json ser/deser is now abstracted away and the :code:`ujson` and
  :code:`rapidjson` exras can be used for somewhat faster block processing.
  (`#11`_)
* multiple other small performance improvements


Version 1.15.0 (27 May 2020)
============================

* switch to 5-byte txnums to handle larger blockchains.  Upgrade DBs during restart.
* accurate clearing of stale caches
* coin additions / updates: NavCoin + Hush + VersusCoin + Zero (cipig), DashRegtest (colmenero),
  Quebecoin (morinpa), Primecoin (Sunny King), multiple (Panagiotis David), RVN (standard-error),
  Sumcoin
* other: Jeremy Rand, Jin Eguchi, ddude, Jonathan Cross, Carsen Klock, cipig


Version 1.14.0 (19 Jan 2020)
============================

* require Python 3.7
* support for Bitcoin SV Genesis activation
* DB upgrade to allow for larger transactions.  Your DB will automatically upgrade when
  starting, the upgrade should take approximately 15 mintues.
* fix server shutdown process
* fix cache race condition (issue `#909`_)
* faster initial sync
* coin additions / updates: Emercoin (yakimka), Feathercoin (wellenreiter01),
  Peercoin (peerchemist), Namecoin (JeremyRand), Zcoin (a-bezrukov), Simplicity,
  Mice (ComputerCraftr), Sibcoin testnet (TriKriSta), Odin (Manbearpixel),
* other: h2o10, osagga, Sombernight, breign, pedr0-fr, wingsuit

Version 1.13.0 (26 Sep 2019)
============================

* daemon: use a single connection for all requests rather than a connection per request.
  Distinguish handling of JSON and HTTP errors
* recognise OP_FALSE OP_RETURN scripts as unspendable
* peers - attempt to bind to correct local IP address
* improve name support (domob1812)
* coin additions / updates: BitZeny (y-chan), ZCoin (a-bezrukov), Emercoin (yakimka),
  BSV (Roger Taylor), Bellcoin (streetcrypto7), Ritocoin (traysi), BTC (Sombernight),
  PIVX (mrcarlanthony), Monacoin (wakiyamap)), NamecoinRegtest (JeremyRand), Axe (ddude1),
  Xaya (domob1812), GZRO (MrNaif2018), Ravencoin (standard-error)
* other: gits7r

Version 1.12.0 (13 May 2019)
============================

* require aiorpcX 0.18.1.  This introduces websocket support.  The environment variables
  changed accordingly; see :envvar:`SERVICES` and :envvar:`REPORT_SERVICES`.
* work around bug in recent versions of uvloop
* aiorpcX upgrade fixes from Shane M
* coin additions / updates: BitcoinSV, Bolivarcoin (Jose Luis Estevez), BTC Testnet (ghost43),
  Odin (Pixxl)

Version 1.11.0 (18 Apr 2019)
============================

* require aiorpcX 0.15.x
* require aiohttp 3.3 or higher; earlier versions had a problematic bug
* add :envvar:`REQUEST_TIMEOUT` and :envvar:`LOG_LEVEL` environment variables
* mark 4 old environment variables obsolete.  ElectrumX won't start until they are removed
* getinfo local RPC cleaned up and shows more stats
* miscellaneous fixes and improvements
* more efficient handling of some RPC methods, particularly
  :func:`blockchain.transaction.get_merkle`
* coin additions / updates: BitcoinSV scaling testnet (Roger Taylor), Dash (zebra lucky),
* issues resolved: `#566`_, `#731`_, `#795`_

Version 1.10.1 (13 Apr 2019)
============================

* introduce per-request costing.  See environment variables documentation for new
  variables :envvar:`COST_SOFT_LIMIT`, :envvar:`COST_HARD_LIMIT`, :envvar:`REQUEST_SLEEP`,
  :envvar:`INITIAL_CONCURRENT`, :envvar:`BANDWIDTH_UNIT_COST`.  Sessions are placed in groups
  with which they share some of their costs.  Prior cost is remembered across reconnects.
* require aiorpcX 0.13.5 for better concurrency handling
* require clients use protocol 1.4 or higher
* handle transaction.get_merkle requests more efficiently (ghost43)
* Windows support (sancoder)
* peers improvements (ghost43)
* report mempool and block sizes in logs
* electrumx_rpc: timeout raised to 30s, fix session request counts
* other tweaks and improvements by Bjorge Dijkstra, ghost43, peleion,
* coin additions / updates: ECA (Jenova7), ECCoin (smogm), GXX (DEVCØN), BZX (2INFINITY),
  DeepOnion (Liam Alford), CivX / EXOS (turcol)

Version 1.10.0 (15 Mar 2019)
============================

* extra countermeasures to limit BTC phishing effectiveness (ghost43)
* peers: mark blacklisted peers bad; force retry blacklisted peers (ghost43)
* coin additions / updates: Monacoin (wakiyamap), Sparks (Mircea Rila), ColossusXT,
  Polis, MNPCoin, Zcoin, GINCoin (cronos), Grosetlcoin (gruve-p), Dash (konez2k),
  Bitsend (David), Ravencoin (standard-error), Onixcoin (Jose Estevez), SnowGem
* coin removals: Gobyte, Moneci (cronos)
* minor tweaks by d42
* issues fixed `#660`_ - unclean shutdowns during initial sync

Version 1.9.5 (08 Feb 2019)
===========================

* server blacklist logic (ecdsa)
* require aiorpcX 0.10.4
* remove dead wallet code
* fix `#727`_ - not listing same peer twice

Version 1.9.4 (07 Feb 2019)
===========================

* require aiorpcX 0.10.3
* fix `#713`_

Version 1.9.3 (05 Feb 2019)
===========================

* ignore potential sybil peers
* coin additions / updates: BitcoinCashABC (cculianu), Monacoin (wakiyamap)

Version 1.9.2 (03 Feb 2019)
===========================

* restore protocol version 1.2 and send a warning for old BTC Electrum clients that they
  need to upgrade.  This is an attempt to protect users of old versions of Electrum from
  the ongoing phishing attacks
* increase default MAX_SEND for AuxPow Chains.  Truncate AuxPow for block heights covered
  by a checkpoint.  (jeremyrand)
* coin additions / updates: NMC (jeremyrand), Dash (zebra-lucky), PeerCoin (peerchemist),
  BCH testnet (Mark Lundeberg), Unitus (ChekaZ)
* tighter RPC param checking (ghost43)

Version 1.9.1 (11 Jan 2019)
===========================

* fix `#684`_

Version 1.9.0 (10 Jan 2019)
===========================

* minimum protocol version is now 1.4
* coin additions / updates: BitcoinSV, SmartCash (rc125), NIX (phamels), Minexcoin (joesixpack),
  BitcoinABC (mblunderburg), Dash (zebra-lucky), BitcoinABCRegtest (ezegom), AXE (slowdive),
  NOR (flo071), BitcoinPlus (bushsolo), Myriadcoin (cryptapus), Trezarcoin (ChekaZ),
  Bitcoin Diamond (John Shine),
* close `#554`_, `#653`_, `#655`_
* other minor tweaks (Michael Schmoock, Michael Taborsky)


Original author of ElectrumX:

**Neil Booth**  kyuupichan@gmail.com  https://github.com/kyuupichan

This fork maintained by:

**Electrum developers** electrumdev@gmail.com  https://github.com/spesmilo


.. _#554: https://github.com/kyuupichan/electrumx/issues/554
.. _#566: https://github.com/kyuupichan/electrumx/issues/566
.. _#653: https://github.com/kyuupichan/electrumx/issues/653
.. _#655: https://github.com/kyuupichan/electrumx/issues/655
.. _#660: https://github.com/kyuupichan/electrumx/issues/660
.. _#684: https://github.com/kyuupichan/electrumx/issues/684
.. _#713: https://github.com/kyuupichan/electrumx/issues/713
.. _#727: https://github.com/kyuupichan/electrumx/issues/727
.. _#731: https://github.com/kyuupichan/electrumx/issues/731
.. _#795: https://github.com/kyuupichan/electrumx/issues/795
.. _#909: https://github.com/kyuupichan/electrumx/issues/909


.. _#5:   https://github.com/spesmilo/electrumx/pull/5
.. _#11:  https://github.com/spesmilo/electrumx/pull/11
.. _#22:  https://github.com/spesmilo/electrumx/issues/22
.. _#24:  https://github.com/spesmilo/electrumx/pull/24
.. _#35:  https://github.com/spesmilo/electrumx/pull/35
.. _#67:  https://github.com/spesmilo/electrumx/pull/67
.. _#70:  https://github.com/spesmilo/electrumx/pull/70
.. _spesmilo/electrumx#75:  https://github.com/spesmilo/electrumx/pull/75
.. _spesmilo/electrumx#122:  https://github.com/spesmilo/electrumx/pull/122
.. _spesmilo/electrumx#248:  https://github.com/spesmilo/electrumx/pull/248
.. _spesmilo/electrumx#273:  https://github.com/spesmilo/electrumx/pull/273
.. _spesmilo/electrumx#298:  https://github.com/spesmilo/electrumx/pull/298
.. _spesmilo/electrumx#301:  https://github.com/spesmilo/electrumx/pull/301


.. _4b3f6510:  https://github.com/spesmilo/electrumx/commit/4b3f6510e94670a013c1abe6247cdd2b0e7e6f8c
.. _a61136c5:  https://github.com/spesmilo/electrumx/commit/a61136c596d6a0290a6be9d21fb7c095c3cea21e
.. _ca59151d:  https://github.com/spesmilo/electrumx/commit/ca59151d7365aabd2394fb39aac1faa25949b49d
.. _25339328:  https://github.com/spesmilo/electrumx/commit/25339328a7468235071d152f728b214df10d4c56
.. _8198ab99:  https://github.com/spesmilo/electrumx/commit/8198ab99603a7e74083b3569c15dad13850c721e
.. _31489365:  https://github.com/spesmilo/electrumx/commit/314893655a7cc0bfcc216b07900fa77b5f66e148
.. _a0d3053f:  https://github.com/spesmilo/electrumx/commit/a0d3053f60074264b54b3ef7583f2600f4b73ead
.. _2969ab11:  https://github.com/spesmilo/electrumx/commit/2969ab110412bebddc3f3e815467fb59538b613d
.. _265a5a87:  https://github.com/spesmilo/electrumx/commit/265a5a87b8ad01f739049c0b1e80923aab318f58
.. _5f4dd2cd:  https://github.com/spesmilo/electrumx/commit/5f4dd2cdb414464484407affbbaae6b7407696cb
.. _f5582b29:  https://github.com/spesmilo/electrumx/commit/f5582b29792625e8cca7cf137a6718c2520bb9cb
.. _0ba87447:  https://github.com/spesmilo/electrumx/commit/0ba87447cb293cfc4a8a26c1c27842b95666875a
