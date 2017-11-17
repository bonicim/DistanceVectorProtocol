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
    print()
    print("THIS IS THE CURRENT FORWARDING TABLE OF ROUTER ", self._router_id)
    print(self._forwarding_table.snapshot(), '\n')
    print("END OF EXECUTION..............ROUTER CALLING FUNCTION AGAIN..............")
    print()
    time.sleep(2)

  def init_router_id(self, router_id):
    if not self._router_id: # this only happens once when this function is called for the first time
      print("Binding socket to localhost and port: ", _ToPort(router_id), "..............", "\n")
      self._socket.bind(('localhost', _ToPort(router_id)))
      self._router_id = router_id

  def init_fwd_tbl(self, f):
    print("Initializing forwarding table..............")
    if not self.is_fwd_table_initialized():
      self.initialize_fwd_table(f)
    else:
      print("Forwarding table already initialized.", '\n')
      print("Updating fwd tbl with most current config file..............")
      self.update_fwd_tbl_with_config_file(f)
    print("Sending initialized/updated fwd tbl to neighbors via UDP..............")
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
        snapshot.append((int(line[0]), int(line[0]), int(line[1])))
    print("This is router: ", self._router_id)
    print("The forwarding table will be initialized to: ", snapshot, '\n')
    self._forwarding_table.reset(snapshot)

  def initialize_curr_config_file(self, config_file):
    """
    :param config_file: The config file for the given router
    :return: void
    """
    print()
    print("Initializing current config file..............")
    with self._lock:
      for line in config_file:
        line = line.strip('\n').split(',')
        tup = (int(line[0]), int(line[1]))
        self._curr_config_file.append(tup)
      print("The current config file shows the following neighbor and cost: ", self._curr_config_file)

  def update_fwd_tbl_with_config_file(self, f):
    fwd_tbl = self._forwarding_table.snapshot()
    config_dict = self.config_to_dict(f)
    acc = []
    for el in fwd_tbl:
      if el[0] in config_dict:
        if el[0] == el[1]:
          print("We have a direct neighbor", el[0], el[1])
          acc.append((el[0], el[0], config_dict[el[0]]))
        else:
          print("We have a neighbor but the indirect way is cheaper", el[0], el[1], el[2])
          acc.append(el)
      else:
        acc.append(el) # adding the source node itself or other indirect nodes
    print("Updating fwd tbl to..............", acc)
    self._forwarding_table.reset(acc)
    self.update_curr_config_file(config_dict)  # this should always match the current config_file

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
    entry_count = len(snapshot)
    msg.extend(struct.pack("!h", entry_count))
    for entry in snapshot:
      msg.extend(struct.pack("!hh", entry[0], entry[2]))
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
    print()
    sock.close()

  def check_for_msg(self):
    print("Listening for new DV updates from neighbors.............")
    msg = self.rcv_dist_vector()
    if msg is not None:
      self.update_fwd_table(msg)
    else:
      print("Fwd tbl NOT updated because router has not received any DV msg's from neighbors.", '\n')

  def rcv_dist_vector(self):
    """
    :return: List of Tuples (id_no, cost) from a neighbor; if no message received, return None.
    """
    read, write, err = select.select([self._socket], [], [], 3)
    if read:
      msg, addr = read[0].recvfrom(_MAX_UPDATE_MSG_SIZE)
      entry_count = int.from_bytes(msg[0:_START_INDEX], byteorder='big')
      dist_vector = []
      for index in range(_START_INDEX, _ToStop(entry_count), _BYTES_PER_ENTRY):
        dist_vector.append(self.get_dest_cost(msg, index))
      return dist_vector
    else:
      return None

  def get_dest_cost(self, msg, index):
    """
    Decodes the destination and cost of a distance vector entry from a router's update message
    :param msg: the UDP message (byte object) sent by a router
    :param index: the current index of the msg
    :return: Tuple in the form of (dest, cost)
    """
    return (int.from_bytes(msg[index:index + _MOVE_TWO_BYTES], byteorder='big'),
            int.from_bytes(msg[index + _MOVE_TWO_BYTES:index + _MOVE_FOUR_BYTES], byteorder='big'))

  def update_fwd_table(self, dist_vector):
    """
    Updates the router's forwarding table based on some distance vector either from config_file or neighbor.
    Returns a list of entries that were changed; otherwise None
    :param dist_vector: List of Tuples (id_no, cost), also could be None
    :return: List of Tuples (id, next_hop, cost)
    """
    try:
      neighbor = self.get_source_node(dist_vector)
      print("Msg from neighbor is: ", dist_vector)
      print("We have a distance vector from neighbor: ", neighbor)
      acc_fwd_tbl = []
      fwd_tbl = self.fwd_tbl_to_dict(self._forwarding_table.snapshot())
      print("Current Fwd Table: ", fwd_tbl, '\n')
      neighbor_cost = fwd_tbl[neighbor][1]
      for dv_entry in dist_vector:
        final_dest = dv_entry[0]
        dv_entry_cost = dv_entry[1]
        dest_cost_via_neighbor = dv_entry_cost + neighbor_cost
        if final_dest in fwd_tbl:
          fwd_tbl_next_hop, fwd_tbl_cost = fwd_tbl[final_dest]
          print("Running Bellman Ford Algorithm for shortest path to: ", final_dest)
          if dv_entry_cost == 0:
            print("This is the neighbor; ignore the cost update.", '\n')
            acc_fwd_tbl.append((final_dest, fwd_tbl_next_hop, fwd_tbl_cost))
          elif fwd_tbl_cost == 0:
            print("This is the actual node. The cost is 0; ignore.", '\n')
            acc_fwd_tbl.append((final_dest, fwd_tbl_next_hop, fwd_tbl_cost))
          elif fwd_tbl_cost < dv_entry_cost:
            print("No update needed. Current cost is still cheaper", '\n')
            acc_fwd_tbl.append((final_dest, fwd_tbl_next_hop, fwd_tbl_cost))
          elif fwd_tbl_cost < dest_cost_via_neighbor:
            print("No update needed. Current cost is still cheaper", '\n')
            acc_fwd_tbl.append((final_dest, fwd_tbl_next_hop, fwd_tbl_cost))
          else:
            print("We have a newer and cheaper route via the neighbor", dest_cost_via_neighbor, '\n')
            acc_fwd_tbl.append((final_dest, neighbor, dest_cost_via_neighbor))
        else:
          print("We have a new dest.", final_dest)
          new_entry = (final_dest, neighbor, dest_cost_via_neighbor)
          print("Adding new entry to fwd tbl: ", new_entry, '\n')
          acc_fwd_tbl.append(new_entry)

      print("Updating fwd table to: ", acc_fwd_tbl, '\n')
      self._forwarding_table.reset(acc_fwd_tbl)
    except:
      print("We should not see this message because the distance vector must come from a neighbor.")
      print("Ideally the router should still keep processing")

  def get_source_node(self, dist_vector):
    """
    :param dist_vector:
    :return: An integer representing a source node OR none
    """
    ret = list(filter(lambda x: x[1] == 0, dist_vector))
    if len(ret) == 1:
      return ret[0][0]
    else:
      raise Exception("Router failed to send a complete DV table.")

  def fwd_tbl_to_dict(self, fwd_tbl):
    return {x[0]: (x[1], x[2]) for x in fwd_tbl}

  def send_update_msg(self):
    msg = self.convert_fwd_table_to_bytes_msg()
    if msg:
      print("Sending DV to neighbors.........")
      self.send_dist_vector_to_neighbors(msg)
    else:
      print("We should never see this output. No message sent. The program must send periodic messages.")

