==================
 Protocol Methods
==================

blockchain.block.header
=======================

Return the block header at the given height.

**Signature**

  .. function:: blockchain.block.header(height, cp_height=0)
  .. versionadded:: 1.3
  .. versionchanged:: 1.4
     *cp_height* parameter added
  .. versionchanged:: 1.4.1

  *height*

    The height of the block, a non-negative integer.

  *cp_height*

    Checkpoint height, a non-negative integer.  Ignored if zero,
    otherwise the following must hold:

      *height* <= *cp_height*

**Result**

  If *cp_height* is zero, the raw block header as a hexadecimal
  string.

  Otherwise a dictionary with the following keys.  This provides a
  proof that the given header is present in the blockchain; presumably
  the client has the merkle root hard-coded as a checkpoint.

  * *branch*

    The merkle branch of *header* up to *root*, deepest pairing first.

  * *header*

    The raw block header as a hexadecimal string.  Starting with version 1.4.1,
    AuxPoW data (if present in the original header) is truncated.

  * *root*

    The merkle root of all blockchain headers up to and including
    *cp_height*.


**Example Result**

With *height* 5 and *cp_height* 0 on the Bitcoin Cash chain:

::

   "0100000085144a84488ea88d221c8bd6c059da090e88f8a2c99690ee55dbba4e00000000e11c48fecdd9e72510ca84f023370c9a38bf91ac5cae88019bee94d24528526344c36649ffff001d1d03e477"

.. _cp_height example:

With *cp_height* 8::

  {
    "branch": [
       "000000004ebadb55ee9096c9a2f8880e09da59c0d68b1c228da88e48844a1485",
       "96cbbc84783888e4cc971ae8acf86dd3c1a419370336bb3c634c97695a8c5ac9",
       "965ac94082cebbcffe458075651e9cc33ce703ab0115c72d9e8b1a9906b2b636",
       "89e5daa6950b895190716dd26054432b564ccdc2868188ba1da76de8e1dc7591"
       ],
    "header": "0100000085144a84488ea88d221c8bd6c059da090e88f8a2c99690ee55dbba4e00000000e11c48fecdd9e72510ca84f023370c9a38bf91ac5cae88019bee94d24528526344c36649ffff001d1d03e477",
    "root": "e347b1c43fd9b5415bf0d92708db8284b78daf4d0e24f9c3405f45feb85e25db"
  }

blockchain.block.headers
========================

Return a concatenated chunk of block headers from the main chain.

**Signature**

  .. function:: blockchain.block.headers(start_height, count, cp_height=0)
  .. versionadded:: 1.2
  .. versionchanged:: 1.4
     *cp_height* parameter added
  .. versionchanged:: 1.4.1

  *start_height*

    The height of the first header requested, a non-negative integer.

  *count*

    The number of headers requested, a non-negative integer.

  *cp_height*

    Checkpoint height, a non-negative integer.  Ignored if zero,
    otherwise the following must hold:

      *start_height* + (*count* - 1) <= *cp_height*

**Result**

  A dictionary with the following members:

  * *count*

    The number of headers returned, between zero and the number
    requested.  If the chain has not extended sufficiently far, only
    the available headers will be returned.  If more headers than
    *max* were requested at most *max* will be returned.

  * *hex*

    The binary block headers concatenated together in-order as a
    hexadecimal string.  Starting with version 1.4.1, AuxPoW data (if present
    in the original header) is truncated if *cp_height* is nonzero.

  * *max*

    The maximum number of headers the server will return in a single
    request.  (Recommended to be at least one difficulty retarget period,
    i.e. 2016)

  The dictionary additionally has the following keys if *count* and
  *cp_height* are not zero.  This provides a proof that all the given
  headers are present in the blockchain; presumably the client has the
  merkle root hard-coded as a checkpoint.

  * *root*

    The merkle root of all blockchain headers up to and including
    *cp_height*.

  * *branch*

    The merkle branch of the last returned header up to *root*,
    deepest pairing first.


**Example Response**

See :ref:`here <cp_height example>` for an example of *root* and
*branch* keys.

::

  {
    "count": 2,
    "hex": "0100000000000000000000000000000000000000000000000000000000000000000000003ba3edfd7a7b12b27ac72c3e67768f617fc81bc3888a51323a9fb8aa4b1e5e4a29ab5f49ffff001d1dac2b7c010000006fe28c0ab6f1b372c1a6a246ae63f74f931e8365e15a089c68d6190000000000982051fd1e4ba744bbbe680e1fee14677ba1a3c3540bf7b1cdb606e857233e0e61bc6649ffff001d01e36299"
    "max": 2016
  }

blockchain.estimatefee
======================

Return the estimated transaction fee per kilobyte for a transaction to
be confirmed within a certain number of blocks.

**Signature**

  .. function:: blockchain.estimatefee(number)

  *number*

    The number of blocks to target for confirmation.

**Result**

  The estimated transaction fee in coin units per kilobyte, as a
  floating point number.  If the daemon does not have enough
  information to make an estimate, the integer ``-1`` is returned.

**Example Result**

::

  0.00101079


blockchain.headers.subscribe
============================

Subscribe to receive block headers when a new block is found.

**Signature**

  .. function:: blockchain.headers.subscribe()

**Result**

  The header of the current block chain tip.  The result is a dictionary with two members:

  * *hex*

    The binary header as a hexadecimal string.

  * *height*

    The height of the header, an integer.

**Example Result**

::

   {
     "height": 520481,
     "hex": "00000020890208a0ae3a3892aa047c5468725846577cfcd9b512b50000000000000000005dc2b02f2d297a9064ee103036c14d678f9afc7e3d9409cf53fd58b82e938e8ecbeca05a2d2103188ce804c4"
   }

**Notifications**

  As this is a subscription, the client will receive a notification
  when a new block is found.  The notification's signature is:

    .. function:: blockchain.headers.subscribe(header)
       :noindex:

    * *header*

      See **Result** above.

.. note:: should a new block arrive quickly, perhaps while the server
  is still processing prior blocks, the server may only notify of the
  most recent chain tip.  The protocol does not guarantee notification
  of all intermediate block headers.

  In a similar way the client must be prepared to handle chain
  reorganisations.  Should a re-org happen the new chain tip will not
  sit directly on top of the prior chain tip.  The client must be able
  to figure out the common ancestor block and request any missing
  block headers to acquire a consistent view of the chain state.


blockchain.relayfee
===================

Return the minimum fee a low-priority transaction must pay in order to
be accepted to the daemon's memory pool.

**Signature**

  .. function:: blockchain.relayfee()

**Result**

  The fee in whole coin units (BTC, not satoshis for Bitcoin) as a
  floating point number.

**Example Results**

::

   1e-05

::

   0.0

blockchain.scripthash.get_balance
=================================

Return the confirmed and unconfirmed balances of a :ref:`script hash
<script hashes>`.

**Signature**

  .. function:: blockchain.scripthash.get_balance(scripthash)
  .. versionadded:: 1.1

  *scripthash*

    The script hash as a hexadecimal string.

**Result**

  A dictionary with keys `confirmed` and `unconfirmed`.  The value of
  each is the appropriate balance in minimum coin units (satoshis).

**Result Example**

::

  {
    "confirmed": 103873966,
    "unconfirmed": 23684400
  }

blockchain.scripthash.get_history
=================================

Return the confirmed and unconfirmed history of a :ref:`script hash
<script hashes>`.

**Signature**

  .. function:: blockchain.scripthash.get_history(scripthash)
  .. versionadded:: 1.1

  *scripthash*

    The script hash as a hexadecimal string.

**Result**

  A list of confirmed transactions in blockchain order, with the
  output of :func:`blockchain.scripthash.get_mempool` appended to the
  list.  Each confirmed transaction is a dictionary with the following
  keys:

  * *height*

    The integer height of the block the transaction was confirmed in.

  * *tx_hash*

    The transaction hash in hexadecimal.

  See :func:`blockchain.scripthash.get_mempool` for how mempool
  transactions are returned.

**Result Examples**

::

  [
    {
      "height": 200004,
      "tx_hash": "acc3758bd2a26f869fcc67d48ff30b96464d476bca82c1cd6656e7d506816412"
    },
    {
      "height": 215008,
      "tx_hash": "f3e1bf48975b8d6060a9de8884296abb80be618dc00ae3cb2f6cee3085e09403"
    }
  ]

::

  [
    {
      "fee": 20000,
      "height": 0,
      "tx_hash": "9fbed79a1e970343fcd39f4a2d830a6bde6de0754ed2da70f489d0303ed558ec"
    }
  ]

blockchain.scripthash.get_mempool
=================================

Return the unconfirmed transactions of a :ref:`script hash <script
hashes>`.

**Signature**

  .. function:: blockchain.scripthash.get_mempool(scripthash)
  .. versionadded:: 1.1

  *scripthash*

    The script hash as a hexadecimal string.

**Result**

  A list of mempool transactions in arbitrary order.  Each mempool
  transaction is a dictionary with the following keys:

  * *height*

    ``0`` if all inputs are confirmed, and ``-1`` otherwise.

  * *tx_hash*

    The transaction hash in hexadecimal.

  * *fee*

    The transaction fee in minimum coin units (satoshis).

**Result Example**

::

  [
    {
      "tx_hash": "45381031132c57b2ff1cbe8d8d3920cf9ed25efd9a0beb764bdb2f24c7d1c7e3",
      "height": 0,
      "fee": 24310
    }
  ]


blockchain.scripthash.listunspent
=================================

Return an ordered list of UTXOs sent to a script hash.

**Signature**

  .. function:: blockchain.scripthash.listunspent(scripthash)
  .. versionadded:: 1.1

  *scripthash*

    The script hash as a hexadecimal string.

**Result**

  A list of unspent outputs in blockchain order.  This function takes
  the mempool into account.  Mempool transactions paying to the
  address are included at the end of the list in an undefined order.
  Any output that is spent in the mempool does not appear.  Each
  output is a dictionary with the following keys:

  * *height*

    The integer height of the block the transaction was confirmed in.
    ``0`` if the transaction is in the mempool.

  * *tx_pos*

    The zero-based index of the output in the transaction's list of
    outputs.

  * *tx_hash*

    The output's transaction hash as a hexadecimal string.

  * *value*

    The output's value in minimum coin units (satoshis).

**Result Example**

::

  [
    {
      "tx_pos": 0,
      "value": 45318048,
      "tx_hash": "9f2c45a12db0144909b5db269415f7319179105982ac70ed80d76ea79d923ebf",
      "height": 437146
    },
    {
      "tx_pos": 0,
      "value": 919195,
      "tx_hash": "3d2290c93436a3e964cfc2f0950174d8847b1fbe3946432c4784e168da0f019f",
      "height": 441696
    }
  ]

.. _subscribed:

blockchain.scripthash.subscribe
===============================

Subscribe to a script hash.

**Signature**

  .. function:: blockchain.scripthash.subscribe(scripthash)
  .. versionadded:: 1.1

  *scripthash*

    The script hash as a hexadecimal string.

**Result**

  The :ref:`status <status>` of the script hash.

**Notifications**

  The client will receive a notification when the :ref:`status <status>` of the script
  hash changes.  Its signature is

    .. function:: blockchain.scripthash.subscribe(scripthash, status)
       :noindex:

blockchain.scripthash.unsubscribe
=================================

Unsubscribe from a script hash, preventing future notifications if its :ref:`status
<status>` changes.

**Signature**

  .. function:: blockchain.scripthash.unsubscribe(scripthash)
  .. versionadded:: 1.4.2

  *scripthash*

    The script hash as a hexadecimal string.

**Result**

  Returns :const:`True` if the scripthash was subscribed to, otherwise :const:`False`.
  Note that :const:`False` might be returned even for something subscribed to earlier,
  because the server can drop subscriptions in rare circumstances.

blockchain.transaction.broadcast
================================

Broadcast a transaction to the network.

**Signature**

  .. function:: blockchain.transaction.broadcast(raw_tx)
  .. versionchanged:: 1.1
     errors returned as JSON RPC errors rather than as a result.

  *raw_tx*

    The raw transaction as a hexadecimal string.

**Result**

  The transaction hash as a hexadecimal string.

  **Note** protocol version 1.0 (only) does not respond according to
  the JSON RPC specification if an error occurs.  If the daemon
  rejects the transaction, the result is the error message string from
  the daemon, as if the call were successful.  The client needs to
  determine if an error occurred by comparing the result to the
  expected transaction hash.

**Result Examples**

::

   "a76242fce5753b4212f903ff33ac6fe66f2780f34bdb4b33b175a7815a11a98e"

Protocol version 1.0 returning an error as the result:

::

  "258: txn-mempool-conflict"

blockchain.transaction.get
==========================

Return a raw transaction.

**Signature**

  .. function:: blockchain.transaction.get(tx_hash, verbose=false)
  .. versionchanged:: 1.1
     ignored argument *height* removed
  .. versionchanged:: 1.2
     *verbose* argument added

  *tx_hash*

    The transaction hash as a hexadecimal string.

  *verbose*

    Whether a verbose coin-specific response is required.

**Result**

    If *verbose* is :const:`false`:

       The raw transaction as a hexadecimal string.

    If *verbose* is :const:`true`:

       The result is a coin-specific dictionary -- whatever the coin
       daemon returns when asked for a verbose form of the raw
       transaction.

**Example Results**

When *verbose* is :const:`false`::

  "01000000015bb9142c960a838329694d3fe9ba08c2a6421c5158d8f7044cb7c48006c1b48"
  "4000000006a4730440220229ea5359a63c2b83a713fcc20d8c41b20d48fe639a639d2a824"
  "6a137f29d0fc02201de12de9c056912a4e581a62d12fb5f43ee6c08ed0238c32a1ee76921"
  "3ca8b8b412103bcf9a004f1f7a9a8d8acce7b51c983233d107329ff7c4fb53e44c855dbe1"
  "f6a4feffffff02c6b68200000000001976a9141041fb024bd7a1338ef1959026bbba86006"
  "4fe5f88ac50a8cf00000000001976a91445dac110239a7a3814535c15858b939211f85298"
  "88ac61ee0700"

When *verbose* is :const:`true`::

 {
   "blockhash": "0000000000000000015a4f37ece911e5e3549f988e855548ce7494a0a08b2ad6",
   "blocktime": 1520074861,
   "confirmations": 679,
   "hash": "36a3692a41a8ac60b73f7f41ee23f5c917413e5b2fad9e44b34865bd0d601a3d",
   "hex": "01000000015bb9142c960a838329694d3fe9ba08c2a6421c5158d8f7044cb7c48006c1b484000000006a4730440220229ea5359a63c2b83a713fcc20d8c41b20d48fe639a639d2a8246a137f29d0fc02201de12de9c056912a4e581a62d12fb5f43ee6c08ed0238c32a1ee769213ca8b8b412103bcf9a004f1f7a9a8d8acce7b51c983233d107329ff7c4fb53e44c855dbe1f6a4feffffff02c6b68200000000001976a9141041fb024bd7a1338ef1959026bbba860064fe5f88ac50a8cf00000000001976a91445dac110239a7a3814535c15858b939211f8529888ac61ee0700",
   "locktime": 519777,
   "size": 225,
   "time": 1520074861,
   "txid": "36a3692a41a8ac60b73f7f41ee23f5c917413e5b2fad9e44b34865bd0d601a3d",
   "version": 1,
   "vin": [ {
     "scriptSig": {
       "asm": "30440220229ea5359a63c2b83a713fcc20d8c41b20d48fe639a639d2a8246a137f29d0fc02201de12de9c056912a4e581a62d12fb5f43ee6c08ed0238c32a1ee769213ca8b8b[ALL|FORKID] 03bcf9a004f1f7a9a8d8acce7b51c983233d107329ff7c4fb53e44c855dbe1f6a4",
       "hex": "4730440220229ea5359a63c2b83a713fcc20d8c41b20d48fe639a639d2a8246a137f29d0fc02201de12de9c056912a4e581a62d12fb5f43ee6c08ed0238c32a1ee769213ca8b8b412103bcf9a004f1f7a9a8d8acce7b51c983233d107329ff7c4fb53e44c855dbe1f6a4"
     },
     "sequence": 4294967294,
     "txid": "84b4c10680c4b74c04f7d858511c42a6c208bae93f4d692983830a962c14b95b",
     "vout": 0}],
   "vout": [ { "n": 0,
              "scriptPubKey": { "addresses": [ "12UxrUZ6tyTLoR1rT1N4nuCgS9DDURTJgP"],
                                "asm": "OP_DUP OP_HASH160 1041fb024bd7a1338ef1959026bbba860064fe5f OP_EQUALVERIFY OP_CHECKSIG",
                                "hex": "76a9141041fb024bd7a1338ef1959026bbba860064fe5f88ac",
                                "reqSigs": 1,
                                "type": "pubkeyhash"},
              "value": 0.0856647},
            { "n": 1,
              "scriptPubKey": { "addresses": [ "17NMgYPrguizvpJmB1Sz62ZHeeFydBYbZJ"],
                                "asm": "OP_DUP OP_HASH160 45dac110239a7a3814535c15858b939211f85298 OP_EQUALVERIFY OP_CHECKSIG",
                                "hex": "76a91445dac110239a7a3814535c15858b939211f8529888ac",
                                "reqSigs": 1,
                                "type": "pubkeyhash"},
              "value": 0.1360904}]}

blockchain.transaction.get_merkle
=================================

Return the merkle branch to a confirmed transaction given its hash
and height.

**Signature**

  .. function:: blockchain.transaction.get_merkle(tx_hash, height)

  *tx_hash*

    The transaction hash as a hexadecimal string.

  *height*

    The height at which it was confirmed, an integer.

**Result**

  A dictionary with the following keys:

  * *block_height*

    The height of the block the transaction was confirmed in.

  * *merkle*

    A list of transaction hashes the current hash is paired with,
    recursively, in order to trace up to obtain merkle root of the
    block, deepest pairing first.

  * *pos*

    The 0-based index of the position of the transaction in the
    ordered list of transactions in the block.

**Result Example**

::

  {
    "merkle":
    [
      "713d6c7e6ce7bbea708d61162231eaa8ecb31c4c5dd84f81c20409a90069cb24",
      "03dbaec78d4a52fbaf3c7aa5d3fccd9d8654f323940716ddf5ee2e4bda458fde",
      "e670224b23f156c27993ac3071940c0ff865b812e21e0a162fe7a005d6e57851",
      "369a1619a67c3108a8850118602e3669455c70cdcdb89248b64cc6325575b885",
      "4756688678644dcb27d62931f04013254a62aeee5dec139d1aac9f7b1f318112",
      "7b97e73abc043836fd890555bfce54757d387943a6860e5450525e8e9ab46be5",
      "61505055e8b639b7c64fd58bce6fc5c2378b92e025a02583303f69930091b1c3",
      "27a654ff1895385ac14a574a0415d3bbba9ec23a8774f22ec20d53dd0b5386ff",
      "5312ed87933075e60a9511857d23d460a085f3b6e9e5e565ad2443d223cfccdc",
      "94f60b14a9f106440a197054936e6fb92abbd69d6059b38fdf79b33fc864fca0",
      "2d64851151550e8c4d337f335ee28874401d55b358a66f1bafab2c3e9f48773d"
    ],
    "block_height": 450538,
    "pos": 710
  }

blockchain.transaction.id_from_pos
==================================

Return a transaction hash and optionally a merkle proof,
given a block height and a position in the block.

**Signature**

  .. function:: blockchain.transaction.id_from_pos(height, tx_pos, merkle=false)
  .. versionadded:: 1.4

  *height*

    The main chain block height, a non-negative integer.

  *tx_pos*

    A zero-based index of the transaction in the given block, an integer.

  *merkle*

    Whether a merkle proof should also be returned, a boolean.

**Result**

  If *merkle* is :const:`false`, the transaction hash as a hexadecimal string.
  If :const:`true`, a dictionary with the following keys:

  * *tx_hash*

    The transaction hash as a hexadecimal string.

  * *merkle*

    A list of transaction hashes the current hash is paired with,
    recursively, in order to trace up to obtain merkle root of the
    block, deepest pairing first.

**Example Results**

When *merkle* is :const:`false`::

  "fc12dfcb4723715a456c6984e298e00c479706067da81be969e8085544b0ba08"

When *merkle* is :const:`true`::

  {
    "tx_hash": "fc12dfcb4723715a456c6984e298e00c479706067da81be969e8085544b0ba08",
    "merkle":
    [
      "928c4275dfd6270349e76aa5a49b355eefeb9e31ffbe95dd75fed81d219a23f8",
      "5f35bfb3d5ef2ba19e105dcd976928e675945b9b82d98a93d71cbad0e714d04e",
      "f136bcffeeed8844d54f90fc3ce79ce827cd8f019cf1d18470f72e4680f99207",
      "6539b8ab33cedf98c31d4e5addfe40995ff96c4ea5257620dfbf86b34ce005ab",
      "7ecc598708186b0b5bd10404f5aeb8a1a35fd91d1febbb2aac2d018954885b1e",
      "a263aae6c470b9cde03b90675998ff6116f3132163911fafbeeb7843095d3b41",
      "c203983baffe527edb4da836bc46e3607b9a36fa2c6cb60c1027f0964d971b29",
      "306d89790df94c4632d652d142207f53746729a7809caa1c294b895a76ce34a9",
      "c0b4eff21eea5e7974fe93c62b5aab51ed8f8d3adad4583c7a84a98f9e428f04",
      "f0bd9d2d4c4cf00a1dd7ab3b48bbbb4218477313591284dcc2d7ca0aaa444e8d",
      "503d3349648b985c1b571f59059e4da55a57b0163b08cc50379d73be80c4c8f3"
    ]
  }

mempool.get_fee_histogram
=========================

Return a histogram of the fee rates paid by transactions in the memory
pool, weighted by transaction size.

**Signature**

  .. function:: mempool.get_fee_histogram()
  .. versionadded:: 1.2

**Result**

  The histogram is an array of [*fee*, *vsize*] pairs, where |vsize_n|
  is the cumulative virtual size of mempool transactions with a fee rate
  in the interval [|fee_n1|, |fee_n|], and |fee_n1| > |fee_n|.

  .. |vsize_n| replace:: vsize\ :sub:`n`
  .. |fee_n| replace:: fee\ :sub:`n`
  .. |fee_n1| replace:: fee\ :sub:`n-1`

  Fee intervals may have variable size.  The choice of appropriate
  intervals is currently not part of the protocol.

  *fee* uses sat/vbyte as unit, and must be a non-negative integer or float.

  *vsize* uses vbyte as unit, and must be a non-negative integer.

**Example Results**

::

    [[12, 128812], [4, 92524], [2, 6478638], [1, 22890421]]

::

   [[59.5, 30324], [40.1, 34305], [35.0, 38459], [29.3, 41270], [27.0, 45167], [24.3, 53512], [22.9, 53488], [21.8, 70279], [20.0, 65328], [18.2, 72180], [18.1, 5254], [18.0, 191579], [16.5, 103640], [15.7, 106715], [15.1, 141776], [14.0, 183261], [13.5, 166496], [11.8, 166050], [11.1, 242436], [9.2, 184043], [7.1, 202137], [5.2, 222011], [4.8, 344788], [4.6, 17101], [4.5, 1696864], [4.1, 598001], [4.0, 32688687], [3.9, 505192], [3.8, 38417], [3.7, 2944970], [3.3, 693364], [3.2, 726373], [3.1, 308878], [3.0, 11884957], [2.6, 996967], [2.3, 822802], [2.2, 9075547], [2.1, 12149801], [2.0, 16387874], [1.4, 873120], [1.3, 3493364], [1.1, 2302460], [1.0, 23204633]]


server.add_peer
===============

A newly-started server uses this call to get itself into other servers'
peers lists.  It should not be used by wallet clients.

**Signature**

  .. function:: server.add_peer(features)

  .. versionadded:: 1.1

  * *features*

    The same information that a call to the sender's
    :func:`server.features` RPC call would return.

**Result**

  A boolean indicating whether the request was tentatively accepted.
  The requesting server will appear in :func:`server.peers.subscribe`
  when further sanity checks complete successfully.


server.banner
=============

Return a banner to be shown in the Electrum console.

**Signature**

  .. function:: server.banner()

**Result**

  A string.

**Example Result**

  ::

     "Welcome to Electrum!"


server.donation_address
=======================

Return a server donation address.

**Signature**

  .. function:: server.donation_address()

**Result**

  A string.

**Example Result**

  ::

     "1BWwXJH3q6PRsizBkSGm2Uw4Sz1urZ5sCj"


server.features
===============

Return a list of features and services supported by the server.

**Signature**

  .. function:: server.features()

**Result**

  A dictionary of keys and values.  Each key represents a feature or
  service of the server, and the value gives additional information.

  The following features MUST be reported by the server.  Additional
  key-value pairs may be returned.

  * *hosts*

    A dictionary, keyed by host name, that this server can be reached
    at.  Normally this will only have a single entry; other entries
    can be used in case there are other connection routes (e.g. Tor).

    The value for a host is itself a dictionary, with the following
    optional keys:

    * *ssl_port*

      An integer.  Omit or set to :const:`null` if SSL connectivity
      is not provided.

    * *tcp_port*

      An integer.  Omit or set to :const:`null` if TCP connectivity is
      not provided.

    A server should ignore information provided about any host other
    than the one it connected to.

  * *genesis_hash*

    The hash of the genesis block.  This is used to detect if a peer
    is connected to one serving a different network.

  * *hash_function*

    The hash function the server uses for :ref:`script hashing
    <script hashes>`.  The client must use this function to hash
    pay-to-scripts to produce script hashes to send to the server.
    The default is "sha256".  "sha256" is currently the only
    acceptable value.

  * *server_version*

    A string that identifies the server software.  Should be the same
    as the first element of the result to the :func:`server.version` RPC call.

  * *protocol_max*
  * *protocol_min*

    Strings that are the minimum and maximum Electrum protocol
    versions this server speaks.  Example: "1.1".

  * *pruning*

    An integer, the pruning limit.  Omit or set to :const:`null` if
    there is no pruning limit.  Should be the same as what would
    suffix the letter ``p`` in the IRC real name.

**Example Result**

::

  {
      "genesis_hash": "000000000933ea01ad0ee984209779baaec3ced90fa3f408719526f8d77f4943",
      "hosts": {"14.3.140.101": {"tcp_port": 51001, "ssl_port": 51002}},
      "protocol_max": "1.0",
      "protocol_min": "1.0",
      "pruning": null,
      "server_version": "ElectrumX 1.0.17",
      "hash_function": "sha256"
  }


server.peers.subscribe
======================

Return a list of peer servers.  Despite the name this is not a
subscription and the server must send no notifications.

**Signature**

  .. function:: server.peers.subscribe()

**Result**

  An array of peer servers, each returned as a 3-element array.  For
  example::

    ["107.150.45.210",
     "e.anonyhost.org",
     ["v1.0", "p10000", "t", "s995"]]

  The first element is the IP address, the second is the host name
  (which might also be an IP address), and the third is a list of
  server features.  Each feature and starts with a letter.  'v'
  indicates the server maximum protocol version, 'p' its pruning limit
  and is omitted if it does not prune, 't' is the TCP port number, and
  's' is the SSL port number.  If a port is not given for 's' or 't'
  the default port for the coin network is implied.  If 's' or 't' is
  missing then the server does not support that transport.

server.ping
===========

Ping the server to ensure it is responding, and to keep the session
alive.  The server may disconnect clients that have sent no requests
for roughly 10 minutes.

**Signature**

  .. function:: server.ping()
  .. versionadded:: 1.2

**Result**

  Returns :const:`null`.

server.version
==============

Identify the client to the server and negotiate the protocol version.
Only the first :func:`server.version` message is accepted.

**Signature**

  .. function:: server.version(client_name="", protocol_version="1.4")

  * *client_name*

    A string identifying the connecting client software.

  * *protocol_version*

    An array ``[protocol_min, protocol_max]``, each of which is a
    string.  If ``protocol_min`` and ``protocol_max`` are the same,
    they can be passed as a single string rather than as an array of
    two strings, as for the default value.

  The server should use the highest protocol version both support::

    version = min(client.protocol_max, server.protocol_max)

  If this is below the value::

    max(client.protocol_min, server.protocol_min)

  then there is no protocol version in common and the server must
  close the connection.  Otherwise it should send a response
  appropriate for that protocol version.

**Result**

  An array of 2 strings:

     ``[server_software_version, protocol_version]``

  identifying the server and the protocol version that will be used
  for future communication.

**Example**::

  server.version("Electrum 3.0.6", ["1.1", "1.2"])

**Example Result**::

  ["ElectrumX 1.2.1", "1.2"]


Masternode methods (Dash and compatible coins)
==============================================


masternode.announce.broadcast
=============================

Pass through the masternode announce message to be broadcast by the daemon.

Whenever a masternode comes online or a client is syncing, they will
send this message which describes the masternode entry and how to
validate messages from it.

**Signature**

  .. function:: masternode.announce.broadcast(signmnb)

  * *signmnb*

    Signed masternode broadcast message in hexadecimal format.

**Result**

  :const:`true` if the message was broadcasted successfully otherwise
  :const:`false`.

**Example**::

  masternode.announce.broadcast("012b825a65a24e2eb8edadbe27c4716dab993bf1046a66da77268ec87dbdd9dfc80100000000ffffffff00000000000000000000ffff22db1fec42d82103bfc9e296bcf4d63eced97b204df8f7b2b90131d452abd2b50909fa2ce6f66d752103bfc9e296bcf4d63eced97b204df8f7b2b90131d452abd2b50909fa2ce6f66d754120e95f74e9c242776df88a586bd52d2bd1838b600e5f3ce9d45d04865ff39a994632d617e810a4480ce24c882980746bc517a92be027d2ea70e4baece33a763608b1f91e5b00000000451201002b825a65a24e2eb8edadbe27c4716dab993bf1046a66da77268ec87dbdd9dfc80100000000ffffffff57280bc007121a0db854998f72e9a9fd2a690f38abffbd9aa94256330c020000b0f91e5b00000000412027c03b1531ee14db6160a62a0cc8b1a7e93ae122bbc6f2dffec721e0ae308b0e19e68523dd429450612bda3a616b56411b4e35d098e25b7c83f19fd2d8537e970000000000000000")

**Example Result**::

  true

masternode.subscribe
====================

Returns the status of masternode.

**Signature**

  .. function:: masternode.subscribe(collateral)

  * *collateral*

    The txId and the index of the collateral.

    A masternode collateral is a transaction with a specific amount of
    coins, it's also known as a masternode identifier.

    i.e. for DASH the required amount is 1,000 DASH or for $PAC is
    500,000 $PAC.

**Result**

  As this is a subscription, the client will receive a notification
  when the masternode status changes.

  The status depends on the server the masternode is hosted, the
  internet connection, the offline time and even the collateral
  amount, so this subscription notice these changes to the user.

**Example**::

  masternode.subscribe("8c59133e714797650cf69043d05e409bbf45670eed7c4e4a386e52c46f1b5e24-0")

**Example Result**::

  {'method': 'masternode.subscribe', u'jsonrpc': u'2.0', u'result': u'ENABLED', 'params': ['8c59133e714797650cf69043d05e409bbf45670eed7c4e4a386e52c46f1b5e24-0'], u'id': 19}

masternode.list
===============

Returns the list of masternodes.

**Signature**

  .. function:: masternode.list(payees)

  * *payees*

    An array of masternode payee addresses.

**Result**

  An array with the masternodes information.

**Example**::

  masternode.list("['PDFHmjKLvSGdnWgDJSJX49Rrh0SJtRANcE',
  'PDFHmjKLvSGdnWgDJSJX49Rrh0SJtRANcF']")

**Example Result**::

    [
      {
        "vin": "9d298c00dae8b491d6801f50cab2e0037852cb556c5619ddb07c50421x9a31ab",
        "status": "ENABLED",
        "protocol": 70213,
        "payee": "PDFHmjKLvSGdnWgDJSJX49Rrh0SJtRANcE",
        "lastseen": "2018-04-01 12:34",
        "activeseconds": 1258000,
        "lastpaidtime": "2018-03-10 12:29",
        "lastpaidblock": 1234,
        "ip": "1.0.0.1",
        "paymentposition": 184,
        "inselection": true,
        "balance": 510350
      },
      {
        "vin": "9d298c00dae8b491d6801f50cab2e0037852cb556c5619ddb07c50421x9a31ac",
        "status": "ENABLED",
        "protocol": 70213,
        "payee": "PDFHmjKLvSGdnWgDJSJX49Rrh0SJtRANcF",
        "lastseen": "2018-04-01 12:34",
        "activeseconds": 1258000,
        "lastpaidtime": "2018-03-15 05:29",
        "lastpaidblock": 1234,
        "ip": "1.0.0.2",
        "paymentposition": 3333,
        "inselection": false,
        "balance": 520700
      },
      ...,
      ...,
      ...,
      ...
    ]


ProTx methods (Dash DIP3)
==============================================


protx.diff
=============================

Returns a diff between two deterministic masternode lists.
The result also contains proof data.

**Signature**

  .. function:: protx.diff(base_height, height)

  *base_height*

    The starting block height

      *1* <= *base_height*

  *height*

    The ending block height.

      *base_height* <= *height*


**Result**

  A dictionary with deterministic masternode lists diff plus proof data

**Example**::

  protx.diff(1, 20000)

**Example Result**::

    {
      "baseBlockHash": "000000000b866e7fefc7df2b4b37f236175cee9ab6dc925a30c62401d92b7406",
      "blockHash": "0000000005b3f97e0af8c72f9a96eca720237e374ca860938ba0d7a68471c4d6",
      "cbTxMerkleTree": "0200000002c9802d02435cfe09e4253bc1ba4875e9a2f920d5d6adf005d5b9306e5322e6f476d885273422c2fe18e8c420d09484f89eaeee7bb7f4e1ff54bddeb94e099a910103",
      "cbTx": "03000500010000000000000000000000000000000000000000000000000000000000000000ffffffff4b02204e047867335c08fabe6d6d8b2b76b7000000000470393f63424273736170747365743a7265737574736574010000000000000010000015770000000d2f6e6f64655374726174756d2f000000000336c8a119010000001976a914cb594917ad4e5849688ec63f29a0f7f3badb5da688ac6c62c216010000001976a914a3c5284d3cd896815ac815f2dd76a3a71cb3d8e688acba65df02000000001976a9146d649e1c05e89d30809ef39cc8ee1002c0c8c84b88ac00000000260100204e0000b301c3d88e4072305bec5d09e2ed6b836b23af640bcdefd7b8ae7e2ca182dc17",
      "deletedMNs": [
      ],
      "mnList": [
        {
          "proRegTxHash": "6f0bdd7034ce8d3a6976a15e4b4442c274b5c1739fb63fc0a50f01425580e17e",
          "confirmedHash": "000000000be653cd1fbc213239cfec83ca68da657f24cc05305d0be75d34e392",
          "service": "173.61.30.231:19023",
          "pubKeyOperator": "8da7ee1a40750868badef2c17d5385480cae7543f8d4d6e5f3c85b37fdd00a6b4f47726b96e7e7c7a3ea68b5d5cb2196",
          "keyIDVoting": "b35c75cbc69433175d3459843e1f6ebe145bf6a3",
          "isValid": true
        }
      ],
      "merkleRootMNList": "17dc82a12c7eaeb8d7efcd0b64af236b836bede2095dec5b3072408ed8c301b3"
    }

protx.info
=============================

Returns detailed information about a deterministic masternode.

**Signature**

  .. function:: protx.info(protx_hash)

  *protx_hash*

    The hash of the initial ProRegTx.

**Result**

  A dictionary with detailed deterministic masternode data

**Example**::

  protx.info("6f0bdd7034ce8d3a6976a15e4b4442c274b5c1739fb63fc0a50f01425580e17e")

**Example Result**::

  {
    "proTxHash": "6f0bdd7034ce8d3a6976a15e4b4442c274b5c1739fb63fc0a50f01425580e17e",
    "collateralHash": "b41439376b6117aebe6ad1ce31dcd217d4934fd00c104029ecb7d21c11d17c94",
    "collateralIndex": 3,
    "operatorReward": 0,
    "state": {
      "registeredHeight": 19525,
      "lastPaidHeight": 20436,
      "PoSePenalty": 0,
      "PoSeRevivedHeight": -1,
      "PoSeBanHeight": -1,
      "revocationReason": 0,
      "keyIDOwner": "b35c75cbc69433175d3459843e1f6ebe145bf6a3",
      "pubKeyOperator": "8da7ee1a40750868badef2c17d5385480cae7543f8d4d6e5f3c85b37fdd00a6b4f47726b96e7e7c7a3ea68b5d5cb2196",
      "keyIDVoting": "b35c75cbc69433175d3459843e1f6ebe145bf6a3",
      "ownerKeyAddr": "ybGQ7a6e7dkJY2jxdbDwdBtyjKZJ8VB7YC",
      "votingKeyAddr": "ybGQ7a6e7dkJY2jxdbDwdBtyjKZJ8VB7YC",
      "addr": "173.61.30.231:19023",
      "payoutAddress": "yWdXnYxGbouNoo8yMvcbZmZ3Gdp6BpySxL"
    },
    "confirmations": 984
  }

Name methods (Namecoin and compatible coins)
==============================================


blockchain.name.get_value_proof
===============================

Returns a name resolution proof, suitable for low-latency (single round-trip) resolution.

**Signature**

  .. function:: blockchain.name.get_value_proof(scripthash, cp_height)
  .. versionadded:: 1.4.3

  *scripthash*

    Script hash of the name being resolved.

  *cp_height*

    Checkpoint height.


**Result**

  A dictionary with transaction and proof data for each transaction associated with the name, from the most recent update back to either the registration transaction or a checkpointed transaction (whichever is later).

**Example**::

  blockchain.name.get_value_proof(bdd490728e7f1cbea1836919db5e932cce651a82f5a13aa18a5267c979c95d3c, 518353)

**Example Result**::

    {
      "bdd490728e7f1cbea1836919db5e932cce651a82f5a13aa18a5267c979c95d3c": [
        {
          "height": 607853,
          "tx": "00710000000102e3d236710b0a21cb9bb4c11d2a2ac6730e6e3c776773f688a53c14fc89ced5ab0100000000ffffffff261a3e3b04326e0dd33208d23e3119144f8cce8394de5757374a37323ee1b6060100000000ffffffff0240420f0000000000415309642f626974636f696e1d76657269667920323032322d30342d313620393a30353a3030204345546d7500144e5cafde0d7fd4f31323e14182a9c3412e026d88978d4e1400000000160014d1a61d11d434ca9197ae47b59a5f8933682140eb02473044022018129841dbd0e9700702b94dfaebee9f00a5e5dc847156faf992195c384a73a502204802754d4a7f406b3fb538d7f2a6c96967deb61eb87597e27dd30410b25e363401210261c92aa521e0e9660d605fff64129ada7faa6f2a201d9f82fd71b4a7ccd0cc230247304402200495ff83f6d2edc697abd3530eb16a315366cf4383225708826ab98c471e8a4b02207ef49122589aaf009e6c7515e04a2e5179263d6efb0f7043d871d3024aae136e0121020138a32ab1be1ae4ee0bf08a2dd6e7208f4e39f98312eebb09016a3fdf31978100000000",
          "tx_merkle": {
            "merkle": [
              "5b08bf9b8e8bff946da742641fc27c107096af5a05c0dcdec1848c3f7423a746",
              "11c88190df88b334f2ce38e7034c1d1caec2e9fccb226ff3229c6440b7a94e6f",
              "ce68ac63e88da5554218cdd7e6811088b1189ab8635bda557044c0518d174d1d"
            ],
            "pos": 1
          }
        },
        {
          "height": 597196,
          "tx": "00710000000102c575b325030f89a7515596c2661967b02ff5a2594ba0a4774833d6bd6bdd41ca0000000000feffffff8afb31ffd43385ff4477c28d81f0dd43e6fc6cc63fe56f7d87733c1de7d67eaa0000000000feffffff028327374a010000001600142715d587d4c8e3edddbac11417b736964c519c6440420f00000000002c5309642f626974636f696e0872657365727665646d750014e893563837316a53cb00c99e13a8079f311173a9024730440220597930ab6071d009ec05e14a9247cbbae4f31c7e3c4e812a995e873eb31646ac02204236ad5e2ee4b72e25428c3e5d70dd9dc387e96bd5dab1f921c6a3dbe7b3f4a8012103549f0ee91a8ce86087fc05862fb06247affca4688d4c6ab166eb9d1531357da90247304402204f48be0eab589fb08acc3cd9281f2c23a742c7745f374176978d7ff1ccf3fd39022052c66eaabab908d7c332083fc88cf863bcf0aea574a1aba6b2dc8106bf0c455a012103877c7f65da9ecc3bd126ee9126b9a03569c2eaebd9b9ef7a30989327321231a7cb1c0900",
          "tx_merkle": {
            "merkle": [
              "bee7bcd954879e9110b019617f443a31f51210b4c841a577abc84dae583a115e",
              "e412f9f6832238fc209fddb2f7aeeedb939e252e0b4a8f7df4b3c509d2b0233e",
              "63e144e0a0726cf8cc9a8c776e6411fc5dfb7f225730bddba0c6a3ab656b3bcd",
              "b5c75bfaaec4c2029b5b175168f9b91af766581dd8496d1184a44e432b20d1fb",
              "4905bf1dc9fbedb16ccacd26a0f406fc62c53b860ac099369010ec610521c4af",
              "3e74dbd7730487df73e07c1fa03b25355c82fbbbd81b35bf28cc8322dda9f3bb"
            ],
            "pos": 38
          }
        },
        {
          "height": 563168,
          "tx": "007100000001026bfeb4ee7d7a5ab62b2e16abf439173a460ac013ee19a0115c26569673eb71eb0000000000feffffff6cee0ee75d60d1a02671d7d2e15e8a8c19045e15d9c6738f76395e8de63ccca30000000000feffffff0240420f00000000002c5309642f626974636f696e0872657365727665646d750014b1cfea8dbd1de3e2e0640b42838f1078f73c050d44d13f4a01000000160014d40f8f5b62c73e2167540001aa1634e60c27fa5a0247304402200207688a4c8e1ca3c093a81f862a0192a94590b2ff4e829564b164ca82b9fada02203ff970582e49ac3d0a8452d211903ba8405153ac556f0a46cfea2d0f3faff182012103cac6fecff4c3a06c71e429dc29a6bea58e42adcc283e5f98e66cb550d6d145df02473044022009e2ec71addf23c21df3034561997aac6e5c7700ac6dd44fcba6157afb865ca202203ad3ff0484f94a405c175184fa9a9ccb6fc8ceab67edc1dba314432e76757d6d01210289706af81a776c60a088ae35021142325f17f81da5778667a172dd1a92e1a644df970800",
          "tx_merkle": {
            "merkle": [
              "9ed4efd8e6b1cf9773db60ef0ad7213ce4282f56df709dd8c69ab2caf466f23b",
              "5cb725f78154e33288b724a59cd6c0146ffb8d0e39ace6f1333479a6eafcb76c",
              "596f00ade273423324821e88c8b99fbe7274470d04c062a0453c7ec1db1086d2",
              "7dfcb41cabce612dc5ab7c4309ada78dbab9dc977321147b2a82ddaae7eb5d02",
              "45d7679dc5e8f92f314b9776ac93e0dcb92015c989c076326511d5a5be5e4a17",
              "ef5096cd39456fe5850c221ad6ec722e11820aa4f6e1773c380e6718e68625e7"
            ],
            "pos": 26
          }
        },
        {
          "height": 528949,
          "tx": "0071000000010234f902ef0f953862bb9ba5b6e9fc1a8a914d8b5b1b9af89e843d85176318c5ff0000000000feffffff24802c9ae06cd5af3dd11bdb653b5df4e9cc6a7bb27b4f1cac13a16fd5249e9b0000000000feffffff0240420f00000000002c5309642f626974636f696e0872657365727665646d750014004b1245eb3ac8724d0bc684e15859c9d5eb9ddabfa39e4a0100000016001440fb148f07ff3c8b1d801628754d8ec5cbe1f7e70247304402203ec1e1907ecefd90180b1f7f9fec4a10b75fdb21a85f0df28ea8fe48419338760220549bebe6b96869eeb3175de8149cd127a42bdb344945bd32571950a1e5df40210121020d0bb22a3730f1de67b87af7e2e5517c7649932e4ad8105817372fd99ca1ce8102473044022013d238876040649429f29f5df08712a3b7439db436e7c7a42e38a2d8c733d05402201475420bfea26e684a4bd488ba6ef0b954ca703234d1536b7a545c55d53759e501210321307ece7f4731fef37b0208807ce1a04d1013c765fef1567752df06e5d7bfe233120800",
          "tx_merkle": {
            "merkle": [
              "0c1a965febb58cd57e8019e6b437f5124968b15855c3066c60a36ae863a900a6",
              "643623318b6409171edb487dc6f14ea56db8956c0bc958abf524ddcf845fd805",
              "1eeb4f63067c4b9ccba24eb717887b3b1f7b95f60ab461b04a785b57251d62d9",
              "94baa94fe1153cc616420bb17bbad79591b409405f8d670b8a1f188e1eda3bc8",
              "bdc4cc1c882e039866939b0ac0c923d1581de93fba1ff1140d3ddc616d1a768d",
              "ac9dbfac19c216f593a932dafe813c3a9ad1d706fdeb2406a0c39db0209cba2c"
            ],
            "pos": 42
          }
        },
        {
          "height": 495000,
          "tx": "007100000001020489c88ef8f718c9ac94bef1424986bfeaa79ec1b66e28841d3de4c3e8238e7f000000006a47304402200b3c3b859fe978f69cadb516e563122ef27fb68dcfea641cbfe69b4c71866cda02206a56c2e1d113e2032cb67a3b506a74ebbdaabca51b3601d9e269042ccce2c3ba012102184feeaa11d4a2ecc0f5c34c10222f6c858d33148c330ad115515d697c77d01efeffffffda3ec65cd0f1d8a5e3b56237993f53c5e1cacc9adec82a808a718023e3592c9a0000000000feffffff0240420f00000000002c5309642f626974636f696e0872657365727665646d75001417e6be040f4b08e4e161db3c27c1dc46b235bba8135a750100000000160014b9667261ba2f8127240b1de27515734bdd5761c00002473044022077d5fc03bdcdd0ced102c2b04ef67d8d9398cc03243f70cef59cbcdae6df0bc202200510a7bc5dc8d8a89dcc16575041912e9fa8913846c1c4ec796c95400ef346d301210330b9946e3a7aaf15858119423ca8cec8bb71e71267e8531feccbeb941abe2e17978d0700",
          "tx_merkle": {
            "merkle": [
              "b87f44bf986dccd425caf731ab403f5d423689977ac15a8d6204e0c8bbf00cf7",
              "f1182408914fa2c86f935ed14a9925a4a20f077b56610131f995b45230155713",
              "78d090050c52d44bf36a99ee4b884a95b6da8fb764fc6e383c6f7b5a4a8ff8be",
              "523dc9ce6021858807700836f65b2b3bf8ea701d723b979afff6ea0cf096815f",
              "0c7c131ab0bf11ba122af39d63a4483b4dbc845a33b507bce7cc0bcefee6d724",
              "a2e5f09a5663c567471571d91c2f8cbd4270d286e772067573439f93e8b4befa",
              "820fbb93b424b3e67602ce2b4136764080f5329b46bc671b97553e2340588e0c"
            ],
            "pos": 11
          },
          "header": {
            "header": "040101003c59eef5329201b1f077d6f8b832b8200cbe9aff82feb75cf5e5bf3240eed8750768161b62133b7b758fe6a724362e3e5948ff4ba3e59098336d057a24d8bd4c37a7535e2394131700000000",
            "branch": [
              "c28a169077edcec70fbb74dac6c64229e862a286fcc22f8695eeb268e7b454a1",
              "7de3af1dce3faf8c93f40608f257a00c042cd13a2b7f5635454079931254602e",
              "9e1f18e383256e49872da0ed2affac378baba7566f2ea0abf6b06d156b331d91",
              "52f09f8d938efcc083643da4171ff0eb77a48dfbbe6ba801494caeb33dd8f2f4",
              "a591862302cb03aebf74fcf9bf552d0ebe50634816719459669532e1f5122d19",
              "2f17efd7c29f3e5f2fc10d494a4dc8cd1e1346fc6848b5f8f053d6133b56af5e",
              "6b1656ebb4b82071b85a6bb6bbf64106f7f9cbd60531d8bbfa665d2d02eae9b0",
              "1841b9a4bc6782948ec92bdbb93c0c8a330728052547f1b2ecd38a2adcff310b",
              "8f5675ecfbdc76c4516f6b110e43b3dd66adb73d2d7f01957055973b832210fa",
              "ede6544bcc09e29229bfaf7d099503172a7826174473816805891b5381dcae3e",
              "9c9a8d5d2d3a7888bedc7906d22ebdd73f684cbc14c435aa4543ad18d56a4ef7",
              "98f0e303e64ddfce18ee0ea9aaf95cecc0d6b849c2d6eb772aba3bf984660601",
              "96c3e6226d5608ccf0a8f3e02d20a258044c9fedb726b041eb94d1d3112cfeb0",
              "f71672e8b1d7621fbf2462e89b0c8d5af91efee7c9de54587acb7d705a8d4659",
              "f9188789442cfeff1955b5030d82317fd77133524bcc4be4e3c24bd857a4925e",
              "7b920859b50d0030c18725888aac05e7edea5bb4f4d3cadd5d6e71fe52a9ebac",
              "e1185d87453a4392ff4d4aea556597bc1b0a1a6320714f364410a73aa136d7aa",
              "345a13ed174cc91593862162fab8610ed84d5dd9cfc1533fd604d812cb13a699",
              "88191322193a9f6ae984009b694edd97670795c8afb130082cf90cd4640e5701"
            ],
            "root": "476a138d228b66a094c20d5bbaea3230ea60201ba16af81500c5f368b06dc48b"
          }
        }
      ]
    }
