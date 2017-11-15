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

def _ToPort(router_id):
  return _BASE_ID + router_id

def _ToRouterId(port):
  return port - _BASE_ID


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
    # TODO: read and update neighbor link cost info, based upon any updated DV msg's from neighbors
    with open(self._config_filename, 'r') as f:
      line = f.readline()
      router_id = int(line.strip())
      print("Router id: ", router_id, '\n')

      if not self._router_id: # this only happens once when this function is called for the first time
        print("Binding socket to localhost and port: ", _ToPort(router_id), "..............", "\n")
        self._socket.bind(('localhost', _ToPort(router_id)))
        self._router_id = router_id

      if not self.is_fwd_table_initialized():
        print("Initializing forwarding table..............", '\n')
        fwd_tbl = self.initialize_fwd_table(f)
        if fwd_tbl:
          print("Fwd table has been initialized: ", '\n')

        print("Converting table to bytes...............")
        msg = self.convert_fwd_table_to_bytes_msg(fwd_tbl)
        if msg:
          print("Fwd table has been converted to bytes msg: ", msg, '\n')

        print("Sending bytes msg to neighbors......")
        self.send_dist_vector_to_neighbors(msg)
        # self.send_dist_vector_to_neighbors(self.convert_fwd_table_to_bytes_msg(self.initialize_fwd_table(f)))
      else:
        print("Checking if config file has changed..................")
        dist_vector_config = self.is_link_cost_neighbors_changed(f)
        if len(dist_vector_config) > 0:
          print("Link cost to neighbors have changed. Updating forwarding table......")
          ret = self.update_fwd_table(dist_vector_config)
          if len(ret) > 0:
            print("Forwarding table has changed as result of new link costs.")
            print("Converting updated table entries to bytes msg......")
            msg = self.convert_fwd_table_to_bytes_msg(ret)
            print()
            print("Sending updated table entries to neighbors.... ")
            self.send_dist_vector_to_neighbors(msg)
          else:
            print("The forwarding table has not changed. No need to send dist vector to neighbors.")
        else:
          print("The config file has NOT changed; the number of changed neighbor costs is: ", len(dist_vector_config), '\n')

      print("Listening for new DV updates from neighbors.............")
      # TODO: implement all functions used below
      dist_vector = self.rcv_dist_vector()
      if dist_vector:
         print("Received update from neighbors", '\n')
         print("Updating forwarding table with neighbor DV.")
         self.update_fwd_table(dist_vector)
         print("Forwarding table has been updated.", '\n')
      else:
         print("Forwarding table has NOT been updated because router has not received any DV msg's from neighbors.", '\n')

      print("Converting forwarding table to bytes msg............", '\n')
      msg = self.convert_fwd_table_to_bytes_msg(self._forwarding_table.snapshot())
      print("Sending forwarding table to neighbors.........", '\n', '\n')
      self.send_dist_vector_to_neighbors(msg)


  def is_link_cost_neighbors_changed(self, config_file):
    """
    :param config_file: A router's neighbor's and cost
    :return: Returns a List of of Tuples (id_no, cost)
    """
    config_file_dict = {}
    for line in config_file:
      line = line.strip("\n")
      list_line = line.split(",")
      config_file_dict[int(list_line[0])] = int(list_line[1])
    ret = []
    with self._lock:
      for neighbor in self._curr_config_file:
        print("Checking cost of neighbor: ", neighbor[0])
        if neighbor[0] in config_file_dict:
          delta = neighbor[1] - config_file_dict[neighbor[0]]
          print("Cost delta is: ", delta)
          if delta > 0:
            ret.append((neighbor[0], config_file_dict[neighbor[0]]))
    return ret

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
    print("The forwarding table will be initialized to: ", snapshot)
    self._forwarding_table.reset(snapshot)
    return snapshot

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

  def convert_fwd_table_to_bytes_msg(self, snapshot):
    """
    :param snapshot: List of Tuples (id, next_hop, cost)
    :return: Bytes object representation of a forwarding table; the format of the message is
    "Entry count, id_no, cost, ..., id_no, cost"
    """
    msg = bytearray()
    print("Converting forwarding table into a bytes object: ", snapshot)
    entry_count = len(snapshot)
    print("The number of entries is: ", entry_count)
    msg.extend(struct.pack("!h", entry_count))
    for entry in snapshot:
      print("Adding destination: ", entry[0], " with cost of: ", entry[2])
      dest = entry[0]
      cost = entry[2]
      msg.extend(struct.pack("!hh", dest, cost))
    return msg

  def convert_bytes_msg_to_distance_vector(self, msg):
    """
    :param msg: Bytes object representation of a neighbor's distance vector; the format of the message is
    "Entry count, id_no, cost, ..., id_no, cost"
    :return: List of Tuples (id_no, cost)
    """
    pass

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
        print("Sending vector to neighbor: ", tup[0])
        port = _ToPort(tup[0])
        print("At port: ", port)
        bytes_sent = sock.sendto(msg, ('localhost', port))
        print("Send bytes object with size of: ", bytes_sent)
    sock.close()
    print()

  def rcv_dist_vector(self):
    """
    :return: Bytes object representation of a neighbor's distance vector; the format of the message is
    "Entry count, id_no, cost, ..., id_no, cost". If no message received, receives -1
    """
    # this is where the socket is called to accept a connection
    # TODO
    read, write, err = select.select([self._socket], [], [], 3)
    if read:
      msg, addr = read[0].recvfrom(_MAX_UPDATE_MSG_SIZE)
      print("Received msg:",  msg)
      print("At addr: ", addr)
      return msg
    else:
      return None

  def update_fwd_table(self, dist_vector):
    """
    Updates the router's forwarding table based on some distance vector either from config_file or neighbor.
    Returns a list of entries that were changed; otherwise None
    :param dist_vector: List of Tuples (id_no, cost)
    :return: List of Tuples (id, next_hop, cost)
    """
    # TODO: add print statements
    # get the neighbor node
    neighbor = self.get_source_node(dist_vector)
    acc_fwd_tbl = []
    fwd_tbl = self._forwarding_table.snapshot()

    for dv_entry in dist_vector:
      fwd_tbl_entry = self.is_dest_in_fwd_tbl(dv_entry, fwd_tbl)
      if fwd_tbl_entry:
        updated_entry = self.is_cost_lt_fwd_tbl_entry(dv_entry,fwd_tbl_entry, neighbor)
        if updated_entry:
          updated_entry = self.calc_min_cost(neighbor, dv_entry, fwd_tbl)
          acc_fwd_tbl = self.update_fwd_tbl_with_new_entry(updated_entry, acc_fwd_tbl)
      else:
        acc_fwd_tbl = self.add_new_entry_to_fwd_tbl(dv_entry, neighbor, acc_fwd_tbl)

    self.overwrite_fwd_tbl(acc_fwd_tbl)
    return self._forwarding_table.__str__()
  
  def get_source_node(self, dist_vector):
    """

    :param dist_vector:
    :return:
    """
    pass

  # TODO implement all helper methods
  def is_dest_in_fwd_tbl(self, dv_entry, fwd_tbl):
    """

    :param dv_entry:
    :param fwd_tbl:
    :return: None or a fwd_tbl_entry, which is a Tuple (dest, hop, cost)
    """
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

