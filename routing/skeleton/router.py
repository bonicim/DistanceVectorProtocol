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
    self._curr_neighbor_cost = []  # a list of (neighbors, next hop, cost) example [(1,1,0), (2,2,9), (3,3,9)]
    self._start_time = None
    self._call_counter = 1
    self._last_msg_sent = None

  def start(self):
    # Start a periodic closure to update config.
    self._config_updater = util.PeriodicClosure(
        self.load_config, _CONFIG_UPDATE_INTERVAL_SEC)
    self._start_time = time.time()
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
    with open(self._config_filename, 'r') as f:
      router_id = int(f.readline().strip())
      self.greeting(router_id)
      self.init_router_id(router_id)
      self.init_fwd_tbl(f)
      self.update_fwd_tbl_with_config_file(f)
      self.send_update_msg_to_neighbors()
    self.check_for_update_msg()
    self.send_update_msg_to_neighbors()
    self.status()

  def init_router_id(self, router_id):
    if not self._router_id: # this only happens once when this function is called for the first time
      print("Binding socket to localhost and port: ", _ToPort(router_id), "..............")
      self._socket.bind(('localhost', _ToPort(router_id)))
      self._router_id = router_id

  def init_fwd_tbl(self, f):
    print("Initializing forwarding table..............")
    if not self.is_fwd_table_initialized():
      with self._lock:
        self._curr_neighbor_cost = []
        for line in f:
          line = line.strip('\n').split(',')
          entry = (int(line[0]), int(line[0]), int(line[1]))
          self._curr_neighbor_cost.append(entry)
        self._curr_neighbor_cost.append((self._router_id, self._router_id, 0))
      print("The forwarding table will be initialized to: ", self._curr_neighbor_cost)
      self._forwarding_table.reset(self._curr_neighbor_cost)
    else:
      print("Forwarding table already initialized.")
      print("Updating fwd tbl with most current config file..............")

  def is_fwd_table_initialized(self):
    return self._forwarding_table.size() > 0

  def update_fwd_tbl_with_config_file(self, f):
    print("Updating curr neighbor_cost..............")
    self.update_curr_neighbor_cost(f)  # this should always match the current config_file
    print("Updating fwd table with current config file")
    snapshot = self._forwarding_table.snapshot()
    neighbor_cost_dict = self.curr_neighbor_cost_to_dict()
    new_snapshot = []
    link_cost_change_flag = False
    for route in snapshot:
      if self.is_neighbor_cost_changed(route, neighbor_cost_dict):
        new_snapshot.append((route[0], route[1], neighbor_cost_dict[route[0]]))
        link_cost_change_flag = True
      else:
        new_snapshot.append(route)
    if link_cost_change_flag:
      print("A neighbor cost has changed.")
      print("Forwarding table updating to: ")
      print(new_snapshot)
      self._forwarding_table.reset(new_snapshot)

  def is_neighbor_cost_changed(self, route, neighbor_cost_dict):
    return route[0] in neighbor_cost_dict and route[2] != neighbor_cost_dict[route[0]]

  def curr_neighbor_cost_to_dict(self):
    with self._lock:
      curr_neighbor_cost = self._curr_neighbor_cost
    return {x[0]: x[2] for x in curr_neighbor_cost}

  def update_curr_neighbor_cost(self, f):
    # only updates the link cost of current neihbors, DOES NOT ADD NEW NEIGHBORS FROM A NEW CONFIG FILE
    # TODO: extend the future to allow adding neighbors to a router via new config files
    config_dict = self.config_to_dict(f)
    with self._lock:
      self._curr_neighbor_cost = list(map(lambda x: (x[0], x[0], config_dict[x[0]]) if x[0] in config_dict else x, self._curr_neighbor_cost))

  def config_to_dict(self, config_file):
    """
    :param config_file: A router's neighbor's and cost
    :return: Returns a Map consisting of K:V pair of id_no : (next, cost)
    """
    config_file_dict = {}
    for line in config_file:
      line = line.strip("\n")
      list_line = line.split(",")
      config_file_dict[int(list_line[0])] = int(list_line[1])
    return config_file_dict

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
    list_msg = []
    for entry in snapshot:
      dest, next_hop, cost = entry
      if dest == next_hop:
        list_msg.append((dest, cost))
        msg.extend(struct.pack("!hh", dest, cost))
      else:
        cost = 42.2
        list_msg.append((dest, cost))
        msg.extend(struct.pack("!he", dest, cost))
    with self._lock:
      self._last_msg_sent = list_msg
    return msg

  def send_update_msg_to_neighbors(self):
    """
    :param msg: Bytes object representation of a node's distance vector; the format of the message is
    "Entry count, id_no, cost, ..., id_no, cost". Upon initialization, the router sends its complete
    forwarding table. But later messages will only send updated entries of its forwarding table, not
    the entire table.
    :return: None or Error
    """
    print("Sending update msg to neighbors via UDP..............")
    msg = self.convert_fwd_table_to_bytes_msg()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    with self._lock:
      for tup in self._curr_neighbor_cost:
        port = _ToPort(tup[0])
        if tup[0] != self._router_id:
          print("Sending distance vector to neighbor and port: ", tup[0], " ", port)
          sock.sendto(msg, ('localhost', port))
    sock.close()

  def check_for_update_msg(self):
    print("Listening for distance vector update msg from neighbors.............")
    msg = self.receive_update_msg()
    if msg is not None:
      self.update_fwd_table(msg)
    else:
      print("No DV msg received. Fwd table not updated.")

  def receive_update_msg(self):
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
    dest = int.from_bytes(msg[index:index + _MOVE_TWO_BYTES], byteorder='big')
    cost = int.from_bytes(msg[index + _MOVE_TWO_BYTES:index + _MOVE_FOUR_BYTES], byteorder='big')
    if cost == 42.2:
      cost = float("inf")
    return dest, cost

  def update_fwd_table(self, dist_vector):
    """
    Updates the router's forwarding table based on some distance vector either from config_file or neighbor.
    Returns a list of entries that were changed; otherwise None
    :param dist_vector: List of Tuples (id_no, cost), also could be None
    :return: List of Tuples (id, next_hop, cost)
    """
    try:
      neighbor = self.get_source_node(dist_vector)
      print("Received update msg from neighbor: ", neighbor)
      print("Update msg: ", dist_vector)
      fwd_tbl_org = self._forwarding_table.snapshot()
      fwd_tbl_dict = self.fwd_tbl_to_dict(fwd_tbl_org)
      acc_fwd_tbl = self.remove_dv_dest_fwd_tbl(dist_vector, fwd_tbl_org)
      with self._lock:
        neighbor_cost_dict = {x[0]: x[2] for x in self._curr_neighbor_cost}
      print("Config cost dict: ", neighbor_cost_dict)
      neighbor_cost = neighbor_cost_dict[neighbor]  # must come from config_file
      print("NEIGHBORCOST:", neighbor_cost)

      for dv_entry in dist_vector:
        final_dest = dv_entry[0]
        print("final dest", final_dest)
        dv_entry_cost = dv_entry[1]
        print("dv_entry_cost", dv_entry_cost)
        updated_entry = None
        if final_dest == neighbor:
          print("We are examining the neighbor's cost itself, ignore")
          updated_entry = (final_dest, fwd_tbl_dict[final_dest][0], fwd_tbl_dict[final_dest][1])
        elif self.is_infinite_cost(dv_entry_cost):
          print("Inside infinite loop")
          updated_entry = (final_dest, final_dest, neighbor_cost_dict[final_dest])
        else:
          dest_cost_config = neighbor_cost_dict[final_dest]
          dest_cost_dv = dv_entry_cost + neighbor_cost
          if dest_cost_dv < dest_cost_config:
            updated_entry = (final_dest, neighbor, dest_cost_dv)
          else:
            updated_entry = (final_dest, final_dest, dest_cost_config)
        if updated_entry is not None:
          acc_fwd_tbl.append(updated_entry)
        else:
          print("This is bad. Entry must be updated.")
      print("Updated Fwd Table: ", acc_fwd_tbl)
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

  def get_list_via_neighbor(self, neighbor, fwd_tbl):
    ret = []
    for el in fwd_tbl:
      if el[0] != neighbor:
        ret.append(el)
    return ret

  def remove_dv_dest_fwd_tbl(self, dist_vector, fwd_tbl):
    dv_dict = self.tup_2_list_to_dict(dist_vector)
    ret = []
    for entry in fwd_tbl:
      if entry[0] not in dv_dict:
        ret.append(entry)
    return ret

  def tup_2_list_to_dict(self, curr_config_list):
    return {x[0]: x[1] for x in curr_config_list}

  def is_infinite_cost(self, dv_entry_cost):
    print(dv_entry_cost)
    print(float("inf"))
    return dv_entry_cost == float("inf")

  def greeting(self, router_id):
    print("Router id: ", router_id)
    print("CALL COUNTER:", self._call_counter, "..............")
    self._call_counter += 1

  def status(self):
    print()
    print("THIS IS THE CURRENT FORWARDING TABLE OF ROUTER ", self._router_id)
    print(self._forwarding_table.snapshot())
    print("THIS IS THE CURRENT CONFIG FILE")
    with self._lock:
      print(self._curr_neighbor_cost)
    with self._lock:
      print("LAST MESSAGE SENT: ")
      print(self._last_msg_sent)
    elapsed = time.time() - self._start_time
    print("ELAPSED TIME: ", elapsed)
    print("END OF EXECUTION..............ROUTER CALLING FUNCTION AGAIN..............")
    print()
