import os.path
import socket
import table
import threading
import util
import struct
import pprint


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
    print()
    print("Processing router's configuration file...")
    # TODO: read and update neighbor link cost info, based upon any updated DV msg's from neighbors
    with open(self._config_filename, 'r') as f:
      line = f.readline()
      router_id = int(line.strip())
      print("Router id: ", router_id, '\n')

      if not self._router_id: # this only happens once when this function is called for the first time
        self._socket.bind(('localhost', _ToPort(router_id)))
        self._router_id = router_id

      if not self.is_fwd_table_initialized():
        pp = pprint.PrettyPrinter(indent=4)
        print("Initializing forwarding table..............", "\n")
        fwd_tbl = self.initialize_fwd_table(f)
        print("Fwd table has been initialized: ")
        pp.pprint(fwd_tbl)
        print()

        print("Converting table to bytes...............")
        msg = self.convert_fwd_table_to_bytes_msg(fwd_tbl)
        print("Fwd table has been converted to bytes msg: ")
        print(msg, '\n')

        print("Sending bytes msg to neighbors......")
        self.send_dist_vector_to_neighbors(msg)
        # self.send_dist_vector_to_neighbors(self.convert_fwd_table_to_bytes_msg(self.initialize_fwd_table(f)))
      else:
        print("Checking if config file has changed..................")
        dist_vector_config = self.is_link_cost_neighbors_changed(f)
        if len(dist_vector_config) > 0:
          print("Link cost to neighbors have changed. Updating forwarding table......", '\n')
          ret = self.update_fwd_table(dist_vector_config)
          if len(ret) > 0:
            print("Forwarding table has changed as result of new link costs.")
            print("Converting updated table entries to bytes msg......")
            msg = self.convert_fwd_table_to_bytes_msg(ret)
            print("Sending updated table entries to neighbors: ")
            self.send_dist_vector_to_neighbors(msg)
          else:
            print("The forwarding table has not changed. No need to send dist vector to neighbors.")
        else:
          print("The config file has NOT changed; the number of changed neighbor costs is: ", len(dist_vector_config))
      # dist_vector = self.rcv_dist_vector()
      # if dist_vector:
      #   self.send_dist_vector_to_neighbors(self.convert_fwd_table_to_bytes_msg(self.update_fwd_table(dist_vector)))


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
    print("This is the number of entries in the forwarding table (should be empty): ", len(snapshot))
    snapshot.append((self._router_id, self._router_id, 0)) # add source node to forwarding table

    with self._lock:
      for line in self._curr_config_file:
        print("Adding neighbor ", line[0], " with cost of ", line[1])
        snapshot.append((int(line[0]), int(line[0]), int(line[1])))
    print("The forwarding table will be initialized to: ")
    pp = pprint.PrettyPrinter(indent=4)
    pp.pprint(snapshot)
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
      print("The current config file shows the following neighbor and cost: ", self._curr_config_file, "\n")


  def convert_fwd_table_to_bytes_msg(self, fwd_table):
    """
    :param fwd_table: List of Tuples (id, next_hop, cost)
    :return: Bytes object representation of a forwarding table; the format of the message is
    "Entry count, id_no, cost, ..., id_no, cost"
    """
    msg = bytearray()
    print("Converting forwarding table into a bytes object: ", fwd_table)
    entry_count = len(fwd_table)
    print("The number of entries is: ", entry_count)
    msg.extend(struct.pack("!h", entry_count))
    for entry in fwd_table:
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


  def rcv_dist_vector(self):
    """
    :return: Bytes object representation of a neighbor's distance vector; the format of the message is
    "Entry count, id_no, cost, ..., id_no, cost". If no message received, receives -1
    """
    # this is where the socket is called to accept a connection
    pass


  def update_fwd_table(self, dist_vector):
    """
    Updates the router's forwarding table based on some distance vector either from config_file or neighbor.
    Returns a list of entries that were changed; otherwise None
    :param dist_vector: List of Tuples (id_no, cost)
    :return: List of Tuples (id, next_hop, cost)
    """
    return 0






