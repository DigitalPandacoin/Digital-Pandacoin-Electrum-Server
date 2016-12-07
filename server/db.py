# Copyright (c) 2016, Neil Booth
#
# All rights reserved.
#
# See the file "LICENCE" for information about the copyright
# and warranty status of this software.

'''Interface to the blockchain database.'''


import array
import ast
import itertools
import os
from struct import pack, unpack
from bisect import bisect_right
from collections import namedtuple

from lib.util import chunks, formatted_time, LoggedClass
from lib.hash import hash_to_str
from server.storage import open_db
from server.version import VERSION


UTXO = namedtuple("UTXO", "tx_num tx_pos tx_hash height value")

class DB(LoggedClass):
    '''Simple wrapper of the backend database for querying.

    Performs no DB update, though the DB will be cleaned on opening if
    it was shutdown uncleanly.
    '''

    DB_VERSIONS = [3]

    class MissingUTXOError(Exception):
        '''Raised if a mempool tx input UTXO couldn't be found.'''

    class DBError(Exception):
        '''Raised on general DB errors generally indicating corruption.'''

    def __init__(self, env):
        super().__init__()
        self.env = env
        self.coin = env.coin

        self.logger.info('switching current directory to {}'
                         .format(env.db_dir))
        os.chdir(env.db_dir)
        self.logger.info('reorg limit is {:,d} blocks'
                         .format(self.env.reorg_limit))

        self.db = None
        self.reopen_db(True)

        create = self.db_height == -1
        self.headers_file = self.open_file('headers', create)
        self.txcount_file = self.open_file('txcount', create)
        self.tx_hash_file_size = 16 * 1024 * 1024

        # tx_counts[N] has the cumulative number of txs at the end of
        # height N.  So tx_counts[0] is 1 - the genesis coinbase
        self.tx_counts = array.array('I')
        self.txcount_file.seek(0)
        self.tx_counts.fromfile(self.txcount_file, self.db_height + 1)
        if self.tx_counts:
            assert self.db_tx_count == self.tx_counts[-1]
        else:
            assert self.db_tx_count == 0
        self.clean_db()

    def reopen_db(self, first_sync):
        '''Open the database.  If the database is already open, it is
        closed (implicitly via GC) and re-opened.

        Re-open to set the maximum number of open files appropriately.
        '''
        if self.db:
            self.logger.info('closing DB to re-open')
            self.db.close()

        max_open_files = 1024 if first_sync else 256

        # Open DB and metadata files.  Record some of its state.
        db_name = '{}-{}'.format(self.coin.NAME, self.coin.NET)
        self.db = open_db(db_name, self.env.db_engine, max_open_files)
        if self.db.is_new:
            self.logger.info('created new {} database {}'
                             .format(self.env.db_engine, db_name))
        else:
            self.logger.info('successfully opened {} database {} for sync: {}'
                             .format(self.env.db_engine, db_name, first_sync))
        self.read_state()

        if self.first_sync == first_sync:
            self.logger.info('software version: {}'.format(VERSION))
            self.logger.info('DB version: {:d}'.format(self.db_version))
            self.logger.info('coin: {}'.format(self.coin.NAME))
            self.logger.info('network: {}'.format(self.coin.NET))
            self.logger.info('height: {:,d}'.format(self.db_height))
            self.logger.info('tip: {}'.format(hash_to_str(self.db_tip)))
            self.logger.info('tx count: {:,d}'.format(self.db_tx_count))
            if self.first_sync:
                self.logger.info('sync time so far: {}'
                                 .format(formatted_time(self.wall_time)))
        else:
            self.reopen_db(self.first_sync)

    def read_state(self):
        if self.db.is_new:
            self.db_height = -1
            self.db_tx_count = 0
            self.db_tip = b'\0' * 32
            self.db_version = max(self.DB_VERSIONS)
            self.flush_count = 0
            self.utxo_flush_count = 0
            self.wall_time = 0
            self.first_sync = True
        else:
            state = self.db.get(b'state')
            if state:
                state = ast.literal_eval(state.decode())
            if not isinstance(state, dict):
                raise self.DBError('failed reading state from DB')
            self.db_version = state['db_version']
            if self.db_version not in self.DB_VERSIONS:
                raise self.DBError('your DB version is {} but this software '
                                   'only handles versions {}'
                                   .format(self.db_version, self.DB_VERSIONS))
            if state['genesis'] != self.coin.GENESIS_HASH:
                raise self.DBError('DB genesis hash {} does not match coin {}'
                                   .format(state['genesis_hash'],
                                           self.coin.GENESIS_HASH))
            self.db_height = state['height']
            self.db_tx_count = state['tx_count']
            self.db_tip = state['tip']
            self.flush_count = state['flush_count']
            self.utxo_flush_count = state['utxo_flush_count']
            self.wall_time = state['wall_time']
            self.first_sync = state['first_sync']

        if self.flush_count < self.utxo_flush_count:
            raise self.DBError('DB corrupt: flush_count < utxo_flush_count')

    def write_state(self, batch):
        '''Write chain state to the batch.'''
        state = {
            'genesis': self.coin.GENESIS_HASH,
            'height': self.db_height,
            'tx_count': self.db_tx_count,
            'tip': self.db_tip,
            'flush_count': self.flush_count,
            'utxo_flush_count': self.utxo_flush_count,
            'wall_time': self.wall_time,
            'first_sync': self.first_sync,
            'db_version': self.db_version,
        }
        batch.put(b'state', repr(state).encode())

    def clean_db(self):
        '''Clean out stale DB items.

        Stale DB items are excess history flushed since the most
        recent UTXO flush (only happens on unclean shutdown), and aged
        undo information.
        '''
        if self.flush_count > self.utxo_flush_count:
            self.utxo_flush_count = self.flush_count
            self.logger.info('DB shut down uncleanly.  Scanning for '
                             'excess history flushes...')
            history_keys = self.excess_history_keys()
            self.logger.info('deleting {:,d} history entries'
                             .format(len(history_keys)))
        else:
            history_keys = []

        undo_keys = self.stale_undo_keys()
        if undo_keys:
            self.logger.info('deleting {:,d} stale undo entries'
                             .format(len(undo_keys)))

        with self.db.write_batch() as batch:
            batch_delete = batch.delete
            for key in history_keys:
                batch_delete(key)
            for key in undo_keys:
                batch_delete(key)
            self.write_state(batch)

    def excess_history_keys(self):
        prefix = b'H'
        keys = []
        for key, hist in self.db.iterator(prefix=prefix):
            flush_id, = unpack('>H', key[-2:])
            if flush_id > self.utxo_flush_count:
                keys.append(key)
        return keys

    def stale_undo_keys(self):
        prefix = b'U'
        cutoff = self.db_height - self.env.reorg_limit
        keys = []
        for key, hist in self.db.iterator(prefix=prefix):
            height, = unpack('>I', key[-4:])
            if height > cutoff:
                break
            keys.append(key)
        return keys

    def undo_key(self, height):
        '''DB key for undo information at the given height.'''
        return b'U' + pack('>I', height)

    def write_undo_info(self, height, undo_info):
        '''Write out undo information for the current height.'''
        self.db.put(self.undo_key(height), undo_info)

    def read_undo_info(self, height):
        '''Read undo information from a file for the current height.'''
        return self.db.get(self.undo_key(height))

    def open_file(self, filename, create=False):
        '''Open the file name.  Return its handle.'''
        try:
            return open(filename, 'rb+')
        except FileNotFoundError:
            if create:
                return open(filename, 'wb+')
            raise

    def fs_update(self, fs_height, headers, block_tx_hashes):
        '''Write headers, the tx_count array and block tx hashes to disk.

        Their first height is fs_height.  No recorded DB state is
        updated.  These arrays are all append only, so in a crash we
        just pick up again from the DB height.
        '''
        blocks_done = len(self.headers)
        new_height = fs_height + blocks_done
        prior_tx_count = (self.tx_counts[fs_height] if fs_height >= 0 else 0)
        cur_tx_count = self.tx_counts[-1] if self.tx_counts else 0
        txs_done = cur_tx_count - prior_tx_count

        assert len(self.tx_hashes) == blocks_done
        assert len(self.tx_counts) == new_height + 1

        # First the headers
        self.headers_file.seek((fs_height + 1) * self.coin.HEADER_LEN)
        self.headers_file.write(b''.join(headers))
        self.headers_file.flush()

        # Then the tx counts
        self.txcount_file.seek((fs_height + 1) * self.tx_counts.itemsize)
        self.txcount_file.write(self.tx_counts[fs_height + 1:])
        self.txcount_file.flush()

        # Finally the hashes
        hashes = memoryview(b''.join(itertools.chain(*block_tx_hashes)))
        assert len(hashes) % 32 == 0
        assert len(hashes) // 32 == txs_done
        cursor = 0
        file_pos = prior_tx_count * 32
        while cursor < len(hashes):
            file_num, offset = divmod(file_pos, self.tx_hash_file_size)
            size = min(len(hashes) - cursor, self.tx_hash_file_size - offset)
            filename = 'hashes{:04d}'.format(file_num)
            with self.open_file(filename, create=True) as f:
                f.seek(offset)
                f.write(hashes[cursor:cursor + size])
            cursor += size
            file_pos += size

    def read_headers(self, start, count):
        '''Requires count >= 0.'''
        # Read some from disk
        disk_count = min(count, self.db_height + 1 - start)
        if start < 0 or count < 0 or disk_count != count:
            raise self.DBError('{:,d} headers starting at {:,d} not on disk'
                               .format(count, start))
        if disk_count:
            header_len = self.coin.HEADER_LEN
            self.headers_file.seek(start * header_len)
            return self.headers_file.read(disk_count * header_len)
        return b''

    def fs_tx_hash(self, tx_num):
        '''Return a par (tx_hash, tx_height) for the given tx number.

        If the tx_height is not on disk, returns (None, tx_height).'''
        tx_height = bisect_right(self.tx_counts, tx_num)

        if tx_height > self.db_height:
            return None, tx_height

        file_pos = tx_num * 32
        file_num, offset = divmod(file_pos, self.tx_hash_file_size)
        filename = 'hashes{:04d}'.format(file_num)
        with self.open_file(filename) as f:
            f.seek(offset)
            return f.read(32), tx_height

    def fs_block_hashes(self, height, count):
        headers = self.read_headers(height, count)
        # FIXME: move to coins.py
        hlen = self.coin.HEADER_LEN
        return [self.coin.header_hash(header)
                for header in chunks(headers, hlen)]

    @staticmethod
    def _resolve_limit(limit):
        if limit is None:
            return -1
        assert isinstance(limit, int) and limit >= 0
        return limit

    def get_history(self, hash168, limit=1000):
        '''Generator that returns an unpruned, sorted list of (tx_hash,
        height) tuples of confirmed transactions that touched the address,
        earliest in the blockchain first.  Includes both spending and
        receiving transactions.  By default yields at most 1000 entries.
        Set limit to None to get them all.
        '''
        limit = self._resolve_limit(limit)
        prefix = b'H' + hash168
        for key, hist in self.db.iterator(prefix=prefix):
            a = array.array('I')
            a.frombytes(hist)
            for tx_num in a:
                if limit == 0:
                    return
                yield self.fs_tx_hash(tx_num)
                limit -= 1

    def get_balance(self, hash168):
        '''Returns the confirmed balance of an address.'''
        return sum(utxo.value for utxo in self.get_utxos(hash168, limit=None))

    def get_utxos(self, hash168, limit=1000):
        '''Generator that yields all UTXOs for an address sorted in no
        particular order.  By default yields at most 1000 entries.
        Set limit to None to get them all.
        '''
        limit = self._resolve_limit(limit)
        s_unpack = unpack
        # Key: b'u' + address_hash168 + tx_idx + tx_num
        # Value: the UTXO value as a 64-bit unsigned integer
        prefix = b'u' + hash168
        for db_key, db_value in self.db.iterator(prefix=prefix):
            if limit == 0:
                return
            limit -= 1
            tx_pos, tx_num = s_unpack('<HI', db_key[-6:])
            value, = unpack('<Q', db_value)
            tx_hash, height = self.fs_tx_hash(tx_num)
            yield UTXO(tx_num, tx_pos, tx_hash, height, value)

    def get_utxo_hash168(self, tx_hash, index):
        '''Returns the hash168 for a UTXO.

        Used only for electrum client command-line requests.
        '''
        hash168 = None
        if 0 <= index <= 65535:
            idx_packed = pack('<H', index)
            hash168, tx_num_packed = self.db_hash168(tx_hash, idx_packed)
        return hash168

    def db_hash168(self, tx_hash, idx_packed):
        '''Return (hash168, tx_num_packed) for the given TXO.

        Both are None if not found.'''
        # Key: b'h' + compressed_tx_hash + tx_idx + tx_num
        # Value: hash168
        prefix = b'h' + tx_hash[:4] + idx_packed

        # Find which entry, if any, the TX_HASH matches.
        for db_key, hash168 in self.db.iterator(prefix=prefix):
            assert len(hash168) == 21

            tx_num_packed = db_key[-4:]
            tx_num, = unpack('<I', tx_num_packed)
            hash, height = self.fs_tx_hash(tx_num)
            if hash == tx_hash:
                return hash168, tx_num_packed

        return None, None

    def db_utxo_lookup(self, tx_hash, tx_idx):
        '''Given a prevout return a (hash168, value) pair.

        Raises MissingUTXOError if the UTXO is not found.  Used by the
        mempool code.
        '''
        idx_packed = pack('<H', tx_idx)
        hash168, tx_num_packed = self.db_hash168(tx_hash, idx_packed)
        if not hash168:
            # This can happen when the daemon is a block ahead of us
            # and has mempool txs spending outputs from that new block
            raise self.MissingUTXOError

        # Key: b'u' + address_hash168 + tx_idx + tx_num
        # Value: the UTXO value as a 64-bit unsigned integer
        key = b'u' + hash168 + idx_packed + tx_num_packed
        db_value = self.db.get(key)
        if not db_value:
            raise self.DBError('UTXO {} / {:,d} in one table only'
                               .format(hash_to_str(tx_hash), tx_idx))
        value, = unpack('<Q', db_value)
        return hash168, value
