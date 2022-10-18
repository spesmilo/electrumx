.. image:: https://api.cirrus-ci.com/github/spesmilo/electrumx.svg?branch=master
    :target: https://cirrus-ci.com/github/spesmilo/electrumx
.. image:: https://coveralls.io/repos/github/spesmilo/electrumx/badge.svg
    :target: https://coveralls.io/github/spesmilo/electrumx

===============================================
ElectrumX - Reimplementation of electrum-server
===============================================

  :Licence: MIT
  :Language: Python (>= 3.8)
  :Original Author: Neil Booth

This project is a fork of `kyuupichan/electrumx <https://github.com/kyuupichan/electrumx>`_.
The original author dropped support for Bitcoin, which we intend to keep.

ElectrumX allows users to run their own Electrum server. It connects to your
full node and indexes the blockchain, allowing efficient querying of the history of
arbitrary addresses. The server can be exposed publicly, and joined to the public network
of servers via peer discovery. As of May 2020, a significant chunk of the public
Electrum server network runs ElectrumX.

Documentation
=============

See `readthedocs <https://electrumx-spesmilo.readthedocs.io/>`_.

