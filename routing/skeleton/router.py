import os.path
import socket
import table
import threading
import util
import struct
import select
import time
import sys
import _thread


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
    self._fired_threads = []

  def start(self):
    # Start a periodic closure to update config.
    self._config_updater = util.PeriodicClosure(
        self.load_config, _CONFIG_UPDATE_INTERVAL_SEC)
    self._start_time = time.time()
    self._config_updater.start()
    while True:
      self.listener_thread()

  def listener_thread(self):
    # print("Listening for DV update msg...")
    msg, addr = self._socket.recvfrom(_MAX_UPDATE_MSG_SIZE)
    srv = threading.Thread(target=self.process_packet, args=(msg,))
    srv.start()
    # read, write, err = select.select([self._socket], [], [], 3)
    # if read:
    #   msg, addr = read[0].recvfrom(_MAX_UPDATE_MSG_SIZE)
    #   srv = threading.Thread(target=self.process_packet, args=(msg,))
    #   srv.start()
    #   with self._lock:
    #     self._fired_threads.append(srv)

  def stop(self):
    print("Entering stop")
    if self._config_updater:
      self._config_updater.stop()
    self._socket.close()
    self.status()
    sys.exit()

  def process_packet(self, msg):
    try:
      # print("Created thread to process MSG")
      entry_count = int.from_bytes(msg[0:_START_INDEX], byteorder='big')
      dist_vector = []
      for index in range(_START_INDEX, _ToStop(entry_count), _BYTES_PER_ENTRY):
        tup = self.get_dest_cost(msg, index)
        dist_vector.append(tup)
      self.update_fwd_table(dist_vector)
      self.send_update_msg_to_neighbors()
      return 0
    except KeyboardInterrupt:
      print("Ctrl-C signal arrived in thread")
      _thread.interrupt_main()

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
      self.init_router_id(router_id)
      self.greeting(router_id)
      self.init_fwd_tbl(f)
    self.send_update_msg_to_neighbors()
    self.status()
    print("END OF EXECUTION...ROUTER CALLING FUNCTION AGAIN...")
    print()

  def init_router_id(self, router_id):
    if not self._router_id:
      print("We should see this message once.")
      self._router_id = router_id
      print("Binding socket to localhost and port: ", _ToPort(router_id), "...")
      try:
        self._socket.bind(('localhost', _ToPort(router_id)))
      except socket.error:
        print("Failure to bind socket")
        sys.exit()

  def init_fwd_tbl(self, f):
    print("Initializing forwarding table...")
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
      self.send_update_msg_to_neighbors()
    else:
      print("Forwarding table already initialized.")
      print("Updating fwd tbl with most current config file...")
      self.update_fwd_tbl_with_config_file(f)

  def is_fwd_table_initialized(self):
    return self._forwarding_table.size() > 0

  def update_fwd_tbl_with_config_file(self, f):
    old_curr_neighbor_cost_dict = self.curr_neighbor_cost_to_dict()
    if self.update_curr_neighbor_cost(f):
      new_curr_neighbor_cost_dict = self.curr_neighbor_cost_to_dict()
      snapshot = self._forwarding_table.snapshot()
      # print("Config file change. Detected nieghbor cost change...")
      # print("Tbl is currently: ", snapshot)
      new_snapshot = []
      for route in snapshot:
        dest = route[0]
        # print("Examine route: ", route)
        if route[2] == 0:
          new_snapshot.append(route)
        elif dest not in (old_curr_neighbor_cost_dict and new_curr_neighbor_cost_dict):
          new_snapshot.append(route)
          # TODO: Extend router to handle when links disappear
        else:
          self.update_route(route, old_curr_neighbor_cost_dict, new_curr_neighbor_cost_dict, new_snapshot)
      self._forwarding_table.reset(new_snapshot)
      # print("Tbl updated to: ", new_snapshot)

  def update_curr_neighbor_cost(self, f):
    # only updates the link cost of current neihbors, DOES NOT ADD NEW NEIGHBORS FROM A NEW CONFIG FILE
    # TODO: extend the future to allow adding neighbors to a router via new config files
    config_neighbor_cost_dict = self.config_to_dict(f)
    cost_change = False
    with self._lock:
      new_curr_neighbor_cost = []
      for neighbor in self._curr_neighbor_cost:
        # print("NEIGHBR: ", neighbor)
        # print(config_neighbor_cost_dict)
        if self.is_link_cost_change(neighbor, config_neighbor_cost_dict):
          cost_change = True
          self.update_neighbor_cost(neighbor, config_neighbor_cost_dict, new_curr_neighbor_cost)
        else:
          new_curr_neighbor_cost.append(neighbor)
      if cost_change:
        self._curr_neighbor_cost = new_curr_neighbor_cost
    return cost_change

  def is_link_cost_change(self, neighbor, config_neighbor_cost_dict):
    neighbor_id = neighbor[0]
    neighbor_id_cost = neighbor[2]
    return neighbor_id in config_neighbor_cost_dict and neighbor_id_cost != config_neighbor_cost_dict[neighbor_id]

  def update_neighbor_cost(self, neighbor, config_neighbor_cost_dict, new_curr_neighbor_cost):
    new_curr_neighbor_cost.append((neighbor[0], neighbor[1], config_neighbor_cost_dict[neighbor[0]]))

  def update_route(self, route, old_curr_neighbor_cost_dict, new_curr_neighbor_cost_dict, new_snapshot):
    dest = route[0]
    next_hop = route[1]
    # print("NEW TABLE: ", new_curr_neighbor_cost_dict)
    if self.is_direct_route(route, new_curr_neighbor_cost_dict):
      new_snapshot.append((dest, next_hop, new_curr_neighbor_cost_dict[dest]))
    elif self.is_indirect_route_cost_changed(route, old_curr_neighbor_cost_dict, new_curr_neighbor_cost_dict):
      new_snapshot.append(
        (self.get_updated_indirect_route(route, old_curr_neighbor_cost_dict, new_curr_neighbor_cost_dict)))
    else:
      print("We should not see this msg. ERROR IN DV algo logic impl")

  def is_direct_route(self, route, neighbor_cost_dict):
    return route[0] in neighbor_cost_dict and route[0] == route[1]

  def is_indirect_route_cost_changed(self, route, old_curr_link_cost_dict, new_curr_link_cost_dict):
    next_hop = route[1]
    dest = route[0]
    return (dest in old_curr_link_cost_dict and
            dest in new_curr_link_cost_dict and
            next_hop in old_curr_link_cost_dict and
            next_hop in new_curr_link_cost_dict and
            old_curr_link_cost_dict[next_hop] != new_curr_link_cost_dict[next_hop])

  def get_updated_indirect_route(self, route, old_curr_link_cost_dict, new_curr_link_cost_dict):
    next_hop = route[1]
    dest = route[0]
    cost_new_curr_link = new_curr_link_cost_dict[next_hop]
    dest_total_cost_old = route[2]
    cost_old_curr_link = old_curr_link_cost_dict[next_hop]
    cost_to_dest_via_next_hop = dest_total_cost_old - cost_old_curr_link
    dest_indirect_cost = cost_to_dest_via_next_hop + cost_new_curr_link
    dest_direct_cost = new_curr_link_cost_dict[dest]
    if dest_direct_cost <= dest_indirect_cost:
      return dest, dest, dest_direct_cost
    else:
      return dest, next_hop, dest_indirect_cost

  def curr_neighbor_cost_to_dict(self):
    with self._lock:
      curr_neighbor_cost = self._curr_neighbor_cost
    return {x[0]: x[2] for x in curr_neighbor_cost}

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
    snapshot = list(filter(lambda x: x[0] == x[1], snapshot))
    entry_count = len(snapshot)
    msg.extend(struct.pack("!h", entry_count))
    list_msg = []
    for entry in snapshot:
      dest, next_hop, cost = entry
      list_msg.append((dest, cost))

      msg.extend(struct.pack("!hh", dest, cost))
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
    # print("Sending update msg to neighbors via UDP...")
    msg = self.convert_fwd_table_to_bytes_msg()
    try:
      sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except socket.error:
      print("Failure to create socket")
      sys.exit()
    with self._lock:
      for tup in self._curr_neighbor_cost:
        port = _ToPort(tup[0])
        if tup[0] != self._router_id:
          sock.sendto(msg, ('localhost', port))
      # print("Sent MSG: ", self._last_msg_sent)
    sock.close()

  def get_dest_cost(self, msg, index):
    """
    Decodes the destination and cost of a distance vector entry from a router's update message
    :param msg: the UDP message (byte object) sent by a router
    :param index: the current index of the msg
    :return: Tuple in the form of (dest, cost)
    """
    dest = int.from_bytes(msg[index:index + _MOVE_TWO_BYTES], byteorder='big')
    cost = int.from_bytes(msg[index + _MOVE_TWO_BYTES:index + _MOVE_FOUR_BYTES], byteorder='big')
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
      curr_neighbor_cost_dict = self.curr_neighbor_cost_to_dict()
      neighbor_cost = curr_neighbor_cost_dict[neighbor]
      # print("RCVD MSG FROM neighbor, cost: ", neighbor, neighbor_cost)
      # print("mSG: ", dist_vector)
      dist_vector = list(filter(lambda x: x[0] != neighbor, dist_vector))
      # print("MSG: ", dist_vector)
      dist_vector_dict = self.list_tup_2_ele_to_dict(dist_vector)
      snapshot = self._forwarding_table.snapshot()
      new_snapshot = [route for route in snapshot if route[0] not in dist_vector_dict]
      fwd_tbl_dict = self.fwd_tbl_to_dict()

      for dv_entry in dist_vector:
        dest_dv_cost = neighbor_cost + dv_entry[1]
        dest = dv_entry[0]
        # print("Neighbor: ", neighbor)
        # print(".....Evaluating dest, partial cost, total cost: ", dest, dv_entry[1], dest_dv_cost)

        if dest not in fwd_tbl_dict:
          # print("--- We have a new destination to add")
          new_snapshot.append((dest, neighbor, dest_dv_cost))
        else:
          dest_cost = None
          # print("--- Updating current route to destination: ", dest)
          # print("The dest as known in fwd tbl:", fwd_tbl_dict[dest])
          if dest == fwd_tbl_dict[dest][0]:
            # print("------Current route is direct...")
            dest_cost = curr_neighbor_cost_dict[dest]
            if dest_dv_cost < dest_cost:
              # print("---------Indirect route via neigbor is cheaper")
              new_snapshot.append((dest, neighbor, dest_dv_cost))
            else:
              # print("---------Current DIRECT route is still cheaper")
              new_snapshot.append((dest, dest, dest_cost))
          else:
            # print("------Current route is indirect: ")
            next_hop = fwd_tbl_dict[dest][0]
            if next_hop != neighbor:
              # print("------Indirect route NOT via neighbor/sender.")
              # print("------Comparing current indirect route vs neighbor route.")
              dest_cost = fwd_tbl_dict[dest][1]
              if dest_dv_cost < dest_cost:
                # print("---------Neighbor Cheaper")
                new_snapshot.append((dest, neighbor, dest_dv_cost))
              else:
                # print("---------Fwd table cheaper")
                new_snapshot.append((dest, next_hop, dest_cost))
            else:
              # print("------Indirect via neighbor; this must be updated")
              # print("------Neighbor:", neighbor)
              # print("------Fwd tbl entry:", dest, fwd_tbl_dict[dest])
              # print("Neighbor cost: ", dest_dv_cost)
              if dest in curr_neighbor_cost_dict:
                # print("Router has direct link")
                dest_cost = curr_neighbor_cost_dict[dest]
                if dest_dv_cost < dest_cost:
                  # print("Neighbor wins")
                  new_snapshot.append((dest, neighbor, dest_dv_cost))
                else:
                  # print("Direct link wins.")
                  new_snapshot.append((dest,dest, dest_cost))
              else:
                # print("Router NOT have direct link")
                new_snapshot.append((dest, neighbor, dest_dv_cost))
      # print("Updated Fwd Table: ", new_snapshot)
      self._forwarding_table.reset(new_snapshot)
    except:
      print("We should not see this message because the distance vector must come from a neighbor.")

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

  def fwd_tbl_to_dict(self):
    return {x[0]: (x[1], x[2]) for x in self._forwarding_table.snapshot()}

  def list_tup_2_ele_to_dict(self, list_tup_2_ele):
    return {x[0]: x[1] for x in list_tup_2_ele}

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
    print("ELAPSED TIME: ", elapsed, '\n')
