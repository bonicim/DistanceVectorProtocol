import os.path
import socket
import table
import threading
import util
import struct
import select
import time


_CONFIG_UPDATE_INTERVAL_SEC = 5

_MAX_UPDATE_MSG_SIZE = 1024
_BASE_ID = 8000
_BYTES_PER_ENTRY = 4
_PADDING = 1
_START_INDEX = 2
_MOVE_TWO_BYTES = 2
_MOVE_FOUR_BYTES = 4

def _ToPort(router_id):
  return _BASE_ID + router_id

def _ToRouterId(port):
  return port - _BASE_ID

def _ToStop(entry_count):
  return entry_count * _BYTES_PER_ENTRY + _PADDING

class Router:
  def __init__(self, config_filename):
    # ForwardingTable has 3 columns (DestinationId,NextHop,Cost). It's
    # threadsafe.
    self._forwarding_table = table.ForwardingTable()
    # Config file has router_id, neighbors, and link cost to reach
    # them.
    self._config_filename = config_filename
    self._router_id = None
    # Socket used to send/recv update messages (using UDP).
    self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    self._lock = threading.Lock()
    self._curr_config_file = []  # a list of neighbors and cost [(2,4), (3,4)]

  def start(self):
    # Start a periodic closure to update config.
    self._config_updater = util.PeriodicClosure(
        self.load_config, _CONFIG_UPDATE_INTERVAL_SEC)
    self._config_updater.start()
    # TODO: init and start other threads.
    while True: pass

  def stop(self):
    if self._config_updater:
      self._config_updater.stop()
    # TODO: clean up other threads.

  def load_config(self):
    """
    If the forwarding table has not been initialized, initializes the router's forwarding table and then sends
    the initial forwarding table to all its neighbors.

    Regardless of whether the forwarding table has been initialized, the following will occur:
    Update the forwarding table if there is any link cost change to a neighbor.
    Update the forwarding table if receive a distance vector from a neighbor.
    If table is updated, send updated forwarding table to neighbors.

    :return: None
    """
    assert os.path.isfile(self._config_filename)
    print("Processing router's configuration file...")
    with open(self._config_filename, 'r') as f:
      router_id = int(f.readline().strip())
      print("Router id: ", router_id, '\n')
      self.init_router_id(router_id)
      self.init_fwd_tbl(f)
    self.check_for_msg()
    print("Sending Updated DV after checking for neighbor update messages.....")
    self.send_update_msg()

  def init_router_id(self, router_id):
    if not self._router_id: # this only happens once when this function is called for the first time
      print("Binding socket to localhost and port: ", _ToPort(router_id), "..............", "\n")
      self._socket.bind(('localhost', _ToPort(router_id)))
      self._router_id = router_id
    else:
      print("Router already initialized.")

  def init_fwd_tbl(self, f):
    print("Initializing forwarding table..............", '\n')
    if not self.is_fwd_table_initialized():
      self.initialize_fwd_table(f)
    else:
      print("Forwarding table already initialized.")
      print("Updating fwd tbl with most current config file.......")
      self.update_fwd_tbl_with_config_file(f)
    print("Sending initialized/updated fwd tbl to neighbors via UDP......")
    self.send_dist_vector_to_neighbors(self.convert_fwd_table_to_bytes_msg())

  def is_fwd_table_initialized(self):
    return self._forwarding_table.size() > 0

  def initialize_fwd_table(self, config_file):
    """
    Initializes the router's forwarding table based on config_file. Also gives _curr_config_file data.
    :return: List of Tuples (id, next_hop, cost)
    """
    self.initialize_curr_config_file(config_file)
    snapshot = self._forwarding_table.snapshot()
    if len(snapshot) == 0:
      print("This is the number of entries in the forwarding table (should be empty): ", len(snapshot))
      snapshot.append((self._router_id, self._router_id, 0))  # add source node to forwarding table
    else:
      print("WE SHOULD NOT SEE THIS MSG BECAUSE FWD TABLE MUST BE EMPTY WHEN INITIALIZING")

    with self._lock:
      for line in self._curr_config_file:
        print("Adding neighbor ", line[0], " with cost of ", line[1])
        snapshot.append((int(line[0]), int(line[0]), int(line[1])))
    print("The forwarding table will be initialized to: ", snapshot, '\n')
    self._forwarding_table.reset(snapshot)

  def initialize_curr_config_file(self, config_file):
    """
    :param config_file: The config file for the given router
    :return: void
    """
    print("Initializing current config file.....")
    with self._lock:
      for line in config_file:
        line = line.strip('\n').split(',')
        tup = (int(line[0]), int(line[1]))
        print("Adding line to _curr_config_file: ", tup)
        self._curr_config_file.append(tup)
      print("The current config file shows the following neighbor and cost: ", self._curr_config_file, '\n')

  def update_fwd_tbl_with_config_file(self, f):
    fwd_tbl = self._forwarding_table.snapshot()
    config_dict = self.config_to_dict(f)
    updated_fwd_tbl = \
      list(map(lambda el: (el[0], el[0], config_dict[el[0]]) if el[0] in config_dict else el, fwd_tbl))
    print("Updating fwd tbl to: ", updated_fwd_tbl)
    self._forwarding_table.reset(updated_fwd_tbl)
    print("Updating curr_config_file", '\n')
    self.update_curr_config_file(config_dict)

  def config_to_dict(self, config_file):
    """
    :param config_file: A router's neighbor's and cost
    :return: Returns a Map consisting of K:V pair of id_no : cost
    """
    config_file_dict = {}
    for line in config_file:
      line = line.strip("\n")
      list_line = line.split(",")
      config_file_dict[int(list_line[0])] = int(list_line[1])
    return config_file_dict

  def update_curr_config_file(self, config_dict):
    with self._lock:
      self._curr_config_file = list(map(lambda x: (x[0], config_dict[x[0]]) if x[0] in config_dict else x, self._curr_config_file))

  def convert_fwd_table_to_bytes_msg(self):
    """
    :param snapshot: List of Tuples (id, next_hop, cost)
    :return: Bytes object representation of a forwarding table; the format of the message is
    "Entry count, id_no, cost, ..., id_no, cost"
    """
    msg = bytearray()
    snapshot = self._forwarding_table.snapshot()
    print("Converting forwarding table into a bytes object: ", snapshot)
    entry_count = len(snapshot)
    print("The number of entries is: ", entry_count)
    msg.extend(struct.pack("!h", entry_count))
    for entry in snapshot:
      print("Adding destination: ", entry[0], " with cost of: ", entry[2])
      dest = entry[0]
      cost = entry[2]
      msg.extend(struct.pack("!hh", dest, cost))
    if msg:
      print("Fwd table has been converted to bytes msg: ", msg)
    return msg

  def send_dist_vector_to_neighbors(self, msg):
    """
    :param msg: Bytes object representation of a node's distance vector; the format of the message is
    "Entry count, id_no, cost, ..., id_no, cost". Upon initialization, the router sends its complete
    forwarding table. But later messages will only send updated entries of its forwarding table, not
    the entire table.
    :return: None or Error
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    with self._lock:
      for tup in self._curr_config_file:
        port = _ToPort(tup[0])
        print("Sending distance vector to neighbor and port: ", tup[0], " ", port)
        sock.sendto(msg, ('localhost', port))
    sock.close()

  def check_for_msg(self):
    dist_vector = self.receive_update_msg()
    self.update_fwd_table(dist_vector)

  def receive_update_msg(self):
    print('\n', "Listening for new DV updates from neighbors.............")
    dist_vector = self.rcv_dist_vector()
    if dist_vector:
      print("Converted msg into DV List: ", dist_vector)
      return dist_vector
    else:
      return None

  def rcv_dist_vector(self):
    """
    :return: List of Tuples (id_no, cost) from a neighbor; if no message received, return None.
    """
    read, write, err = select.select([self._socket], [], [], 3)
    if read:
      msg, addr = read[0].recvfrom(_MAX_UPDATE_MSG_SIZE)
      print("Received msg:",  msg)
      entry_count = int.from_bytes(msg[0:_START_INDEX], byteorder='big')
      dist_vector = []
      for index in range(_START_INDEX, _ToStop(entry_count), _BYTES_PER_ENTRY):
        dest_cost = self.get_dest_cost(msg, index)
        dist_vector.append(dest_cost)
      return dist_vector
    else:
      return None

  def update_fwd_table(self, dist_vector):
    """
    Updates the router's forwarding table based on some distance vector either from config_file or neighbor.
    Returns a list of entries that were changed; otherwise None
    :param dist_vector: List of Tuples (id_no, cost), also could be None
    :return: List of Tuples (id, next_hop, cost)
    """
    if dist_vector:

      print("Updating forwarding table with neighbor DV.........")
      acc_fwd_tbl = []
      fwd_tbl = self._forwarding_table.snapshot()
      print("Current Fwd Table: ", fwd_tbl)
      neighbor = self.get_source_node(dist_vector)
      if neighbor:
        print("We have a distance vector from neighbor: ", neighbor)
        for dv_entry in dist_vector:
          fwd_tbl_entry = self.is_dest_in_fwd_tbl(dv_entry, fwd_tbl)
          print("Fwd tbl entry: ", fwd_tbl_entry)
            # if fwd_tbl_entry:
            #   updated_entry = self.is_cost_lt_fwd_tbl_entry(dv_entry,fwd_tbl_entry, neighbor)
            #   if updated_entry:
            #     updated_entry = self.calc_min_cost(neighbor, dv_entry, fwd_tbl)
            #     acc_fwd_tbl = self.update_fwd_tbl_with_new_entry(updated_entry, acc_fwd_tbl)
            # else:
            #   acc_fwd_tbl = self.add_new_entry_to_fwd_tbl(dv_entry, neighbor, acc_fwd_tbl)
      else:
        print("We have the most-up-to-date config file.")
        #   for fwd_tbl_entry in fwd_tbl:
        #     updated_entry = self.is_dest_in_dist_vector(fwd_tbl_entry, dist_vector)
        #     if updated_entry:
        #       acc_fwd_tbl = self.add_new_entry_to_fwd_tbl(updated_entry, updated_entry[0], acc_fwd_tbl)
        #     else:
        #       acc_fwd_tbl = self.add_new_entry_to_fwd_tbl(fwd_tbl_entry, fwd_tbl_entry[1], acc_fwd_tbl)
        #
        # self.overwrite_fwd_tbl(acc_fwd_tbl)
        # print("Forwarding table has been updated.", '\n')
        # return self._forwarding_table.__str__()
    else:
      print("Fwd tbl NOT updated because router has not received any DV msg's from neighbors.", '\n')

  def is_dest_in_dist_vector(self, fwd_tbl_entry, dist_vector):
    """

    :param dist_vector: List of Tuples
    :return: The entry in dist_vector if found or None
    """
    pass

  def get_source_node(self, dist_vector):
    """
    :param dist_vector:
    :return: An integer representing a source node OR none
    """
    ret = list(filter(lambda x: x[1] == 0, dist_vector))
    if len(ret) == 1:
      return ret[0][0]
    else:
      return None

  def is_dest_in_fwd_tbl(self, dv_entry, fwd_tbl):
    """

    :param dv_entry: Tuple of (id, cost)
    :param fwd_tbl:
    :return: None or a fwd_tbl_entry, which is a Tuple (dest, hop, cost)
    """
    # go through fwd table and find tbl entry that has dv_entry destination
    # or return None
    pass

  def is_cost_lt_fwd_tbl_entry(self, dv_entry, fwd_tbl_entry, neighbor):
    """

    :param dv_entry:
    :param fwd_tbl_entry:
    :param neighbor
    :return:
    """
    pass

  def calc_min_cost(self, neighbor, dv_entry, fwd_tbl):
    """

    :param neighbor:
    :param dv_entry:
    :param fwd_tbl:
    :return: an updated fwd_tbl_entry, which is a Tuple (dest, next hop, cost)
    """
    pass

  def update_fwd_tbl_with_new_entry(self, updated_entry, acc_fwd_tbl):
    """
    Appends updated entry to acc_fwd_tbl
    :param updated_entry:
    :param acc_fwd_tbl:
    :return: an updated acc_fwd_tbl, which is a List of Tuples (dest, next hop, cost)
    """
    pass

  def add_new_entry_to_fwd_tbl(self, dv_entry, neighbor, acc_fwd_tbl):
    """

    :param dv_entry:
    :param neighbor:
    :param acc_fwd_tbl:
    :return: an updated acc_fwd_tbl, which is a List of Tuples (dest, next hop, cost)
    """
    pass

  def overwrite_fwd_tbl(self, acc_fwd_tbl):
    """

    :param acc_fwd_tbl:
    :return: None
    """
    pass

  def get_dest_cost(self, msg, index):
    """
    Decodes the destination and cost of a distance vector entry from a router's update message
    :param msg: the UDP message (byte object) sent by a router
    :param index: the current index of the msg
    :return: Tuple in the form of (dest, cost)
    """
    return (int.from_bytes(msg[index:index + _MOVE_TWO_BYTES], byteorder='big'),
            int.from_bytes(msg[index + _MOVE_TWO_BYTES:index + _MOVE_FOUR_BYTES], byteorder='big'))

  def send_update_msg(self):
    print("Converting forwarding table to bytes msg............")
    msg = self.convert_fwd_table_to_bytes_msg()
    if msg:
      print("Conversion succeeded; sending forwarding table to neighbors.........")
      self.send_dist_vector_to_neighbors(msg)
    else:
      print("We should never see this output. No message sent. The program must send periodic messages.")

