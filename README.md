# ElectrumX - python electrum server

```
Licence: MIT
Original Author: Neil Booth
Current Maintainers: The Electrum developers
Language: Python (>= 3.10)
```

[![Latest PyPI package](https://badge.fury.io/py/e_x.svg)](https://pypi.org/project/e-x/)
[![Build Status](https://api.cirrus-ci.com/github/spesmilo/electrumx.svg?branch=master)](https://cirrus-ci.com/github/spesmilo/electrumx)
[![Test coverage statistics](https://coveralls.io/repos/github/spesmilo/electrumx/badge.svg?branch=master)](https://coveralls.io/github/spesmilo/electrumx)

This project is a fork of [kyuupichan/electrumx](https://github.com/kyuupichan/electrumx).
The original author dropped support for Bitcoin, which we intend to keep.

ElectrumX allows users to run their own Electrum server. It connects to your
full node and indexes the blockchain, allowing efficient querying of the history of
arbitrary addresses. The server can be exposed publicly, and joined to the public network
of servers via peer discovery. As of May 2020, a significant chunk of the public
Electrum server network runs ElectrumX.

### Documentation

See [readthedocs](https://electrumx-spesmilo.readthedocs.io).

