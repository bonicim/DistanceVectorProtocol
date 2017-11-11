import os.path
import socket
import table
import threading
import util

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


  def start(self):
    # should initilizaiton happen here
    # Start a periodic closure to update config.
    self._config_updater = util.PeriodicClosure(
        self.load_config, _CONFIG_UPDATE_INTERVAL_SEC)
    self._config_updater.start() # after 5 seconds, a thread will be created to run the load_config;
    # then the thread is destroyed, after another 5 seconds, a new thread is created and load_config run again and so on
    # TODO: init and start other threads.      What does this mean?
    while True: pass


  def stop(self):
    if self._config_updater:
      self._config_updater.stop()
    # TODO: clean up other threads.
    # what does this mean???


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
      line = f.readline()
      print("Raw line is: ", line)
      router_id = int(line.strip())
      print("Router id: ", router_id)
      # Only set router_id when first initialize.
      if not self._router_id:
        self._socket.bind(('localhost', _ToPort(router_id)))
        self._router_id = router_id
      # TODO: read and update neighbor link cost info, based upon any updated DV msg's from neighbors
      # initialize fwd table based on config file
      # this is where _forwarding_table is updated based on config file
      # this is where you listen and accept for router neighbor updates and then update your config_file, which is the router's distance vector
      # if there is an updated DV msg, update the config_file accordingly
      # this is where the socket must listen for new messages and update its DV accordingly
      # this is also where socket will send its DV to its neighbors


  def is_link_cost_neighbors_changed(self, config_file):
    pass


  def is_fwd_table_initialized(self):
    pass


  def initialize_fwd_table(self, config_file):
    """
    Initializes the router's forwarding table based on config_file
    :return: List of Tuples (id, next_hop, cost)
    """
    pass


  def convert_fwd_table_to_bytes_msg(self, fwd_table):
    """
    :param fwd_table: List of Tuples (id, next_hop, cost)
    :return: Bytes object representation of a forwarding table; the format of the message is
    "Entry count, id_no, cost, ..., id_no, cost"
    """
    pass


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
    "Entry count, id_no, cost, ..., id_no, cost"
    :return: None or Error
    """
    pass

  def rcv_dist_vector(self):
    """
    :return: Bytes object representation of a neighbor's distance vector; the format of the message is
    "Entry count, id_no, cost, ..., id_no, cost". If no message received, receives -1
    """
    pass


  def update_fwd_table(self, dist_vector):
    """
    Updates the router's forwarding table based on some distance vector either from config_file or neighbor
    :param dist_vector: List of Tuples (id_no, cost)
    :return: List of Tuples (id, next_hop, cost)
    """
    pass




