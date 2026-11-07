from enum import Enum
from ctypes import c_int32, c_uint32
from group import Room
import socket
from utils import read_u32, read_as_u32_list, read_u16, read_u16_be, write_u32, write_u32_list, write_u16, write_u16_be
from uuid import UUID
from utils import GAME_ENCODING

class H5_STREAM_TYPE(Enum):
  """Main file stream type for serialized buffers; names assumed."""
  STRUCTURE = 0
  """Empty."""
  COMPRESSED_DATA = 1
  """Contains serialized data, optionally compressed with zlib."""
  RAW_DATA = 2
  """Empty."""
  METADATA = 3
  """Unused."""
  SERIALIZATION_MODE = 4
  """Contains pointer search serialization mode."""
  LOOKUP_TABLE = 5
  """Empty."""

class H5_Stream:
  """File stream/substream (structure field) for serialized buffers."""
  def __init__(self, buf: bytes = None):
    if buf is None:
      self.id = -1
      self.size = -1
      self.size_4b = False
      self.data = bytes()
    else:
      self.read(buf)

  def read(self, buf: bytes):
    """Returns nb of bytes read."""
    self.id = int(buf[0])
    # 4-byte size
    if int(buf[1]) & 1 == 1:
      self.size_4b = True
      self.size = read_u32(buf[1:]) >> 1
      if len(buf) < self.size + 5:
        raise ValueError("Stream's declared size exceeds the buffer size.")
      self.data = buf[5:self.size + 5]
    # 1-byte size
    else:
      self.size_4b = False
      self.size = int(buf[1]) >> 1
      if len(buf) < self.size + 2:
        raise ValueError("Stream's declared size exceeds the buffer size.")
      self.data = buf[2:self.size + 2]

  def write(self):
    """Returns a serialized buffer."""
    buf = bytearray([self.id])
    if self.size_4b or self.size > 127:
      buf.extend(write_u32((self.size << 1) | 1))
    else:
      buf.append(self.size << 1)
    buf.extend(self.data)
    return bytes(buf)

  def __len__(self):
    return (5 if self.size_4b else 2) + self.size

class H5_IP:
  """`sockaddr_in` structure (16B): address family, port and IPv4 address, both in network byte order."""
  def __init__(self, buf: bytes = None):
    if buf is None:
      self.family = 0
      self.port = 0
      self.ip = "0.0.0.0"
      self.zero = bytes(8)
    else:
      if len(buf) != 16:
        raise ValueError("H5_IP: invalid input, deserialization failed")
      # AF_INET (2) when set
      self.family = read_u16(buf[0:2])
      self.port = read_u16_be(buf[2:4])
      self.ip = socket.inet_ntoa(buf[4:8])
      # sin_zero
      self.zero = buf[8:16]

  def serialize(self):
    """Returns a serialized buffer."""
    buf = bytearray()
    buf.extend(write_u16(self.family))
    buf.extend(write_u16_be(self.port))
    try:
      buf.extend(socket.inet_aton(self.ip))
    except OSError:
      raise ValueError("H5_IP: invalid IPv4 address, serialization failed")
    buf.extend(self.zero)
    return bytes(buf)

  def __repr__(self):
    return f"'{self.ip}:{self.port}'"

class H5_LocalIP:
  """Local (private) IPv4 address of `PlayerInfo` in network byte order, padded to 16B."""
  def __init__(self, buf: bytes = None):
    if buf is None:
      self.ip = "0.0.0.0"
      self.padding = bytes(12)
    else:
      if len(buf) != 16:
        raise ValueError("H5_LocalIP: invalid input, deserialization failed")
      self.ip = socket.inet_ntoa(buf[0:4])
      self.padding = buf[4:16]

  def serialize(self):
    """Returns a serialized buffer."""
    try:
      ip = socket.inet_aton(self.ip)
    except OSError:
      raise ValueError("H5_LocalIP: invalid IPv4 address, serialization failed")
    return ip + self.padding

  def __repr__(self):
    return f"'{self.ip}'"

class H5_PlayerInfo:
  def __init__(self, buf: bytes = None):
    self.key: int = None
    """Id written before the entry in `RoomInfo.player_infos`; `None` numbers the entries from 0."""
    if buf is None:
      self.username = ""
      """idx = 2"""
      self.ext_ip = H5_IP()
      """idx = 3; external (public) address"""
      self.local_port = 0
      """idx = 4; local (private) port"""
      self.local_ip = H5_LocalIP()
      """idx = 4; local (private) IPv4"""
      self.int38 = 0
      """idx = 5; unknown value"""
      self.rest = bytes()
      """The fields after these, as the game wrote them."""
    else:
      # parse buffer
      pos = 0
            
      field = H5_Stream(buf)
      cur_idx = 2
      # empty for player slots nobody took yet
      if field.id == cur_idx:
        self.username = field.data.decode(GAME_ENCODING)
      else:
        raise ValueError(f"PlayerInfo: missing/invalid field - Username ({cur_idx})")
      pos += len(field)

      field = H5_Stream(buf[pos:])
      cur_idx = 3
      if field.id == cur_idx and field.size == 18:
        self.ext_ip = H5_Serializer.read_ipv4(field.data, False)
      else:
        raise ValueError(f"PlayerInfo: missing/invalid field - ExtIP ({cur_idx})")
      pos += len(field)

      field = H5_Stream(buf[pos:])
      cur_idx = 4
      if field.id == cur_idx:
        self.local_port, self.local_ip = H5_PlayerInfo.read_port_ipv4(field.data)
      else:
        raise ValueError(f"PlayerInfo: missing/invalid field - LocalPortIP ({cur_idx})")
      pos += len(field)

      field = H5_Stream(buf[pos:])
      cur_idx = 5
      if field.id == cur_idx and field.size == 4:
        self.int38 = read_u32(field.data)
      else:
        raise ValueError(f"PlayerInfo: missing/invalid field - Int38 ({cur_idx})")
      pos += len(field)
      self.rest = bytes(buf[pos:])

  @staticmethod
  def read_port_ipv4(buf: bytes):
    """Reads port and IPv4 address; compare with `read_ipv4`"""
    # port - idx 2
    field = H5_Stream(buf)
    if field.id == 2 and field.size == 2:
      port = read_u16(field.data)
    else:
      raise ValueError(f"PlayerInfo: missing/invalid field - PortIP.Port ({field.id})")
    # ip - idx 3
    field = H5_Stream(buf[len(field):])
    if field.id == 3 and field.size == 16:
      ip = H5_LocalIP(field.data)
    else:
      raise ValueError(f"PlayerInfo: missing/invalid field - PortIP.IP ({field.id})")
    return port, ip

  @staticmethod
  def write_port_ipv4(port: int, ip: H5_LocalIP):
    """Writes port and IPv4 address; compare with `write_ipv4`"""
    buf = bytearray()
    field = H5_Stream()

    field.id = 2
    field.data = write_u16(port)
    field.size = len(field.data)
    buf.extend(field.write())

    field.id = 3
    field.data = ip.serialize()
    field.size = len(field.data)
    buf.extend(field.write())
    return bytes(buf)

  def serialize(self):
    """Returns a serialized buffer."""
    buf = bytearray()
    field = H5_Stream()

    field.id = 2
    field.data = self.username.encode(GAME_ENCODING, errors="replace")
    field.size = len(field.data)
    buf.extend(field.write())

    field.id = 3
    field.data = H5_Serializer.write_ipv4(self.ext_ip)
    field.size = len(field.data)
    buf.extend(field.write())

    field.id = 4
    field.data = H5_PlayerInfo.write_port_ipv4(self.local_port, self.local_ip)
    field.size = len(field.data)
    buf.extend(field.write())

    field.id = 5
    field.data = write_u32(self.int38)
    field.size = len(field.data)
    buf.extend(field.write())
    buf.extend(self.rest)
    return bytes(buf)

class H5_RoomInfo:
  """Data from `group.Room.group_info` buffer (HoMM5).

  Each game writes its own set of fields: Heroes V 2.1 writes 2-33 and 37-40, Hammers of Fate 2-33 and
  34-36, Tribes of the East 2-11, 13-33 and 34-41. Every field is kept as the game wrote it, in its order
  (`fields`); the server rewrites only the ids, the host's address and the players' entries. The other
  fields it knows are read into the attributes below for information, and go back to the players as they
  came, so a room info of any game is passed on whole."""
  BASE_FIELDS = (2, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29,
    30, 31, 32, 33, 37, 38, 39, 40)
  """Heroes V 2.1's fields, in its order: those of a room info built here (without a buffer)."""
  REWRITTEN = (2, 3, 4, 22)
  """The fields the server sets: the group and lobby server ids, the host's address and the players."""

  def __init__(self, buf: bytes = None):
    self.group_id: int = -1
    """idx = 2"""
    self.lobby_sv_id: int = -1
    """idx = 3"""
    self.host_ip = H5_IP()
    """idx = 4"""
    self.host_logic_init: bool = False
    """idx = 6"""
    self.group_name: str = ""
    """idx = 7"""
    self.password: str = ""
    """idx = 8"""
    self.is_pwd_protected: bool = False
    """idx = 9"""
    self.ghost_mode: bool = False
    """idx = 10"""
    self.quick_combat: bool = False
    """idx = 11"""
    self.fast_combat_turns: bool = False
    """idx = 12"""
    self.time_limit: int = 0
    """idx = 13"""
    self.difficulty_id: int = 0
    """idx = 14"""
    self.map_desc: str = ""
    """idx = 15; map descriptor path (`AdvMapDesc`)"""
    self.teams: list[int] = []
    """idx = 19"""
    self.max_players: int = 0
    """idx = 20"""
    self.map_size: int = 0
    """idx = 21"""
    self.player_infos: list[H5_PlayerInfo] = []
    """idx = 22"""
    self.game_version: int = 0
    """idx = 23"""
    self.is_saved: bool = False
    """idx = 24"""
    self.guid: str = ""
    """idx = 25"""
    self.ubi_send_results_wait: bool = False
    """idx = 26"""
    self.options: dict[str, tuple[int, str]] = {}
    """idx = 27"""
    self.is_arena: bool = False
    """idx = 28"""
    self.host_checksum: int = 0
    """idx = 29"""
    self.adventure_type: int = 0
    """idx = 30"""
    self.map_desc_tag: str = ""
    """idx = 31"""
    self.map_goal: str = ""
    """idx = 32"""
    self.arena_map_name: str = ""
    """idx = 33"""
    self.int104: int = 0
    """idx = 37"""
    self.int108: int = 0
    """idx = 38"""
    self.combat_turn_speed: int = 0
    """idx = 39"""
    self.flag110: bool = False
    """idx = 40"""
    self.fields: list[tuple[int, bytes]] = None
    """Every field (id, data) in the game's order; `None` for a room info built here."""
    if buf is not None:
      self.read(buf)

  def read(self, buf: bytes):
    """Reads the fields of a room info's data stream."""
    self.fields = []
    seen = set()
    pos = 0
    while pos < len(buf):
      field = H5_Stream(buf[pos:])
      pos += len(field)
      known = ROOM_INFO_FIELDS.get(field.id)
      if field.id in H5_RoomInfo.REWRITTEN:
        # written from one value: a second copy would go out unchecked
        if field.id in seen:
          raise ValueError(f"RoomInfo: repeated field ({field.id})")
        seen.add(field.id)
        setattr(self, known[0], known[1](field.data))
      elif known is not None:
        try:
          setattr(self, known[0], known[1](field.data))
        except (ValueError, IndexError, KeyError):
          # another game's field of this id: it goes back as it came all the same
          pass
      self.fields.append((field.id, bytes(field.data)))

  @staticmethod
  def read_nested_string(buf: bytes):
    """Reads a string nested inside idx 2 field."""
    # nested field
    inner_field = H5_Stream(buf)
    idx = 2
    if inner_field.id == idx and inner_field.size > 0:
      return inner_field.data.decode(GAME_ENCODING)
    elif inner_field.size == 0:
      return ""
    else:
      raise ValueError(f"RoomInfo: missing/invalid field - String.Value ({idx})")

  @staticmethod
  def read_uints(buf: bytes):
    """Reads a list of LE uint32s."""
    # nested field - idx 1 (entry count)
    field = H5_Stream(buf)
    if field.id == 1 and field.size == 4:
      # nested field - idx 2 (entries)
      field = H5_Stream(buf[len(field):])
      if field.id == 2:
        return read_as_u32_list(field.data)
      else:
        raise ValueError(f"RoomInfo: missing/invalid field - IDs.Entries (2)")
    else:
      raise ValueError(f"RoomInfo: missing/invalid field - IDs.Count (1)")

  @staticmethod
  def read_player_infos(buf: bytes):
    """Reads a list of player info structs."""
    # nested field - idx 3 (entry count)
    field = H5_Stream(buf)
    if field.id == 3 and field.size == 4:
      count = read_u32(field.data)
      pos = len(field)
      field = H5_Stream(buf[pos:])
      if field.id == 4 and field.size == 4:
        mode_setting_2 = read_u32(field.data)
        # idx 1 - sizes
        pos += len(field)
        sizes: list[int] = []
        for _ in range(count):
          field = H5_Stream(buf[pos:])
          if field.id == 1 and field.size == 4:
            sizes.append(read_u32(field.data))
          else:
            raise ValueError(f"RoomInfo: missing/invalid field - PlayerInfos.Count (1)")  
          pos += len(field)

        player_infos: list[H5_PlayerInfo] = []
        for i in range(count):
          field = H5_Stream(buf[pos:])
          if field.id == 2:
            info = H5_PlayerInfo(field.data)
            info.key = sizes[i]
            player_infos.append(info)
          else:
            raise ValueError(f"RoomInfo: missing/invalid field - PlayerInfos.PlayerInfo (2)")  
          pos += len(field)
        return player_infos
      else:
        raise ValueError(f"RoomInfo: missing/invalid field - PlayerInfos.ModeSetting2 (4)")  
    else:
      raise ValueError(f"RoomInfo: missing/invalid field - PlayerInfos.ModeSetting1 (3)")

  @staticmethod
  def read_options(buf: bytes):
    """Reads options (`map<string, int32, string>`)."""
    # nested field - idx 3 (entry count)
    field = H5_Stream(buf)
    if field.id == 3 and field.size == 4:
      count = read_u32(field.data)
      pos = len(field)
      field = H5_Stream(buf[pos:])
      if field.id == 4 and field.size == 4:
        # unknown value, 0xD (13)
        mode_setting_2 = read_u32(field.data)
        # a number of idx 1 key (string) followed by a list of (idx 2 + idx 3) value tuples (int32, utf16 string)
        options: dict[str, tuple[int, str]] = {}
        keys: list[str] = []
        pos += len(field)
        for _ in range(count):
          field = H5_Stream(buf[pos:])
          if field.id == 1:
            keys.append(field.data.decode(GAME_ENCODING))
          else:
            raise ValueError(f"RoomInfo: missing/invalid field - Options.Key (1)")
          pos += len(field)
        
        values: list[tuple[int, str]] = []
        for _ in range(count):
          field = H5_Stream(buf[pos:])
          # nested field - idx 2
          if field.id == 2:
            int_value, str_value = H5_RoomInfo.read_option_values(field.data)
          else:
            raise ValueError(f"RoomInfo: missing/invalid field - Options.IntValue (2)")
          values.append((int_value, str_value))
          pos += len(field)

        for idx in range(count):
          options[keys[idx]] = (values[idx][0], values[idx][1])

        return options
      else:
        raise ValueError(f"RoomInfo: missing/invalid field - PlayerInfos.ModeSetting2 (4)")  
    else:
      raise ValueError(f"RoomInfo: missing/invalid field - PlayerInfos.ModeSetting1 (3)")

  @staticmethod
  def read_option_values(buf: bytes):
    """Reads `<int, str>` value pair; the string value is in UTF-16."""
    pos = 0
    field = H5_Stream(buf)
    if field.id == 2:
      int_value = read_u32(field.data)
    else:
      raise ValueError(f"RoomInfo: missing/invalid field - Options.IntValue (2)")
    pos += len(field)
    field = H5_Stream(buf[pos:])
    if field.id == 3:
      str_value = field.data.decode("utf-16-le")
    else:
      raise ValueError(f"RoomInfo: missing/invalid field - Options.StringValue (3)")
    return int_value, str_value

  @staticmethod
  def write_nested_string(string: str):
    """Writes a string as nested inside idx 2 field."""
    field = H5_Stream()
    field.id = 2
    field.data = string.encode(GAME_ENCODING, errors="replace")
    field.size = len(field.data)
    return field.write()

  @staticmethod
  def write_uints(lst: list[int]):
    """Writes a list of ints as LE uint32s."""
    buf = bytearray()
    # count - idx 1
    field = H5_Stream()
    field.id = 1
    field.data = write_u32(len(lst))
    field.size = len(field.data)
    buf.extend(field.write())

    # data - idx 2
    field.id = 2
    field.data = write_u32_list(lst)
    field.size = len(field.data)
    buf.extend(field.write())
    return bytes(buf)

  @staticmethod
  def write_player_infos(infos: list[H5_PlayerInfo]):
    """Writes a list of player info structs."""
    buf = bytearray()
    # count - idx 3
    field = H5_Stream()
    field.id = 3
    field.data = write_u32(len(infos))
    field.size = len(field.data)
    buf.extend(field.write())
    # unknown value 13 - idx 4
    field.id = 4
    field.data = write_u32(13)
    field.size = len(field.data)
    buf.extend(field.write())
    data = bytearray()
    some_id = 0
    for info in infos:
      # info entries - idx 2
      field.id = 2
      field.data = info.serialize()
      field.size = len(field.data)
      data.extend(field.write())
      # either ids (starting at 0), positions or zeroes - idx 1
      field.id = 1
      field.data = write_u32(some_id if info.key is None else info.key)
      field.size = len(field.data)
      buf.extend(field.write())
      some_id += 1
    
    buf.extend(data)
    return bytes(buf)

  @staticmethod
  def write_options(options: dict[str, tuple[int, str]]):
    """Writes options (`map<string, int32, string>`)."""
    buf = bytearray()
    # count - idx 3
    field = H5_Stream()
    field.id = 3
    field.data = write_u32(len(options))
    field.size = len(field.data)
    buf.extend(field.write())
    # unknown value 13 - idx 4
    field.id = 4
    field.data = write_u32(13)
    field.size = len(field.data)
    buf.extend(field.write())
    data = bytearray()
    for key in options:
      # keys - idx 1
      field.id = 1
      field.data = key.encode(GAME_ENCODING, errors="replace")
      field.size = len(field.data)
      buf.extend(field.write())
      # values - idx 2
      field.id = 2
      field.data = H5_RoomInfo.write_option_values(options[key])
      field.size = len(field.data)
      data.extend(field.write())

    buf.extend(data)
    return bytes(buf)

  @staticmethod
  def write_option_values(values: tuple[int, str]):
    """Writes `<int, str>` value pair; the string value is in UTF-16."""
    buf = bytearray()
    field = H5_Stream()

    field.id = 2
    field.data = write_u32(values[0])
    field.size = len(field.data)
    buf.extend(field.write())

    field.id = 3
    field.data = values[1].encode("utf-16-le")
    field.size = len(field.data)
    buf.extend(field.write())

    return bytes(buf)
  
  def serialize(self):
    """Returns a serialized buffer: the fields as they came, but those the server sets."""
    fields = self.fields if self.fields is not None else [(idx, None) for idx in H5_RoomInfo.BASE_FIELDS]
    buf = bytearray()
    for idx, data in fields:
      if data is None or idx in H5_RoomInfo.REWRITTEN:
        attr, _, write = ROOM_INFO_FIELDS[idx]
        data = write(getattr(self, attr))
      field = H5_Stream()
      field.id = idx
      field.data = data
      field.size = len(data)
      buf.extend(field.write())
    return bytes(buf)

class H5_Room:
  """Full room information matchmaking object, includes game-specific data."""
  def __init__(self, room: Room, info: H5_RoomInfo):
    self.gs_room = room
    self.room_info = info
    self.match_id: int = None
    """Rated match being played in the room."""
    self.creator = None
    """The lobby connection that created the room; it joins the room next."""
    self.left: dict[str, float] = {}
    """Players who left the room lately (the host announces them), and when."""

class H5_Serializer:
  """Equivalent to the original `CStructureSaver` serializer (HoMM5)."""
  def __init__(self):
    self.streams: dict[str, H5_Stream] = {}

  def serialize_roominfo(self, room_info: H5_RoomInfo):
    """Serializes `CRoomInfo` structure into a `group.Room.group_info` buffer."""
    return self.serialize_struct(room_info.serialize())

  def serialize_playerinfo(self, player_info: H5_PlayerInfo):
    """Serializes `PlayerInfo` structure into a lobby member data buffer (`LOBBY_MSG.SET_PLAYER_INFO`)."""
    return self.serialize_struct(player_info.serialize())

  def serialize_struct(self, data: bytes):
    """Wraps serialized structure fields into the file streams."""
    # stream 0 - empty
    struct_stream = H5_Stream()
    struct_stream.id = H5_STREAM_TYPE.STRUCTURE.value
    struct_stream.size_4b = False
    struct_stream.size = 0
    
    # stream 1 - room info
    data_stream = H5_Stream()
    data_stream.id = H5_STREAM_TYPE.COMPRESSED_DATA.value
    data_stream.data = data
    data_stream.size = len(data_stream.data)
    # the limit for 1B size is 127
    data_stream.size_4b = data_stream.size > 127

    # stream 2 - empty
    raw_data_stream = H5_Stream()
    raw_data_stream.id = H5_STREAM_TYPE.RAW_DATA.value
    raw_data_stream.size_4b = False
    raw_data_stream.size = 0

    # stream 4 - serialization mode (4)
    mode_stream = H5_Stream()
    mode_stream.id = H5_STREAM_TYPE.SERIALIZATION_MODE.value
    mode_stream.size_4b = False
    mode_stream.size = 4
    mode_stream.data = write_u32(4)

    # stream 5 - empty
    table_stream = H5_Stream()
    table_stream.id = H5_STREAM_TYPE.LOOKUP_TABLE.value
    table_stream.size_4b = False
    table_stream.size = 0

    self.streams = {
      H5_STREAM_TYPE.STRUCTURE.name: struct_stream,
      H5_STREAM_TYPE.COMPRESSED_DATA.name: data_stream,
      H5_STREAM_TYPE.RAW_DATA.name: raw_data_stream,
      H5_STREAM_TYPE.SERIALIZATION_MODE.name: mode_stream,
      H5_STREAM_TYPE.LOOKUP_TABLE.name: table_stream
    }

    buf = bytearray(self.streams[H5_STREAM_TYPE.SERIALIZATION_MODE.name].write())
    buf.extend(self.streams[H5_STREAM_TYPE.COMPRESSED_DATA.name].write())
    buf.extend(self.streams[H5_STREAM_TYPE.STRUCTURE.name].write())
    buf.extend(self.streams[H5_STREAM_TYPE.RAW_DATA.name].write())
    buf.extend(self.streams[H5_STREAM_TYPE.LOOKUP_TABLE.name].write())
    return bytes(buf)

  def deserialize_playerinfo(self, buf: bytes) -> H5_PlayerInfo:
    """Reads a `PlayerInfo` structure from a serialized buffer (`LOBBY_MSG.SET_PLAYER_INFO`)."""
    return H5_PlayerInfo(self.data_stream(buf).data)

  def data_stream(self, buf: bytes) -> H5_Stream:
    """Reads the streams of a serialized buffer, returns its data stream."""
    size = len(buf)
    pos = 0
    while pos < size:
      stream = H5_Stream(buf)
      bts_read = len(stream)
      pos += bts_read
      self.streams[H5_STREAM_TYPE(stream.id).name] = stream
      buf = buf[bts_read:]
    data_stream = self.streams.get(H5_STREAM_TYPE.COMPRESSED_DATA.name)
    if data_stream is None:
      raise ValueError("Data stream missing in the buffer.")
    return data_stream

  def deserialize_roominfo(self, buf: bytes) -> H5_RoomInfo:
    """Reads `CRoomInfo` structure from a serialized `group.Room.group_info` buffer."""
    size = len(buf)
    pos = 0
    # streams
    while (pos < size):
      stream = H5_Stream(buf)
      bts_read = len(stream)
      pos += bts_read
      self.streams[H5_STREAM_TYPE(stream.id).name] = stream
      buf = buf[bts_read:]

    data_stream = self.streams[H5_STREAM_TYPE.COMPRESSED_DATA.name]
    if data_stream is None:
      raise ValueError("Data stream missing in RoomInfo buffer.")

    room_info = H5_RoomInfo(data_stream.data)
    return room_info

  @staticmethod
  def read_ipv4(buf: bytes, room_info: bool):
    """Reads `sockaddr_in` address encoded on 16B. Shared between `RoomInfo` and `PlayerInfo`."""
    # nested field - idx 2
    field = H5_Stream(buf)
    if field.id == 2 and field.size == 16:
      return H5_IP(field.data)
    else:
      cl_name = "RoomInfo" if room_info else "PlayerInfo"
      raise ValueError(f"{cl_name}: missing/invalid field - HostIP (2)")

  @staticmethod
  def write_ipv4(ip: H5_IP):
    """Writes `sockaddr_in` address structure encoded on 16B. Shared between `RoomInfo` and `PlayerInfo`."""
    # nested field - idx 2
    field = H5_Stream()
    field.id = 2
    field.data = ip.serialize()
    field.size = len(field.data)
    return field.write()

def _sized(size: int, read):
  """A reader that wants the field to be `size` bytes."""
  def reader(data: bytes):
    if len(data) != size:
      raise ValueError(f"RoomInfo: {len(data)} bytes, not {size}")
    return read(data)
  return reader

def _read_guid(data: bytes):
  return f"{data[:4].hex()}-{data[4:6].hex()}-{data[6:8].hex()}-{data[8:10].hex()}-{data[10:].hex()}"

_bool = (_sized(1, lambda d: d[0] != 0), lambda v: bytes([1 if v else 0]))
_int32 = (_sized(4, lambda d: c_int32(read_u32(d)).value), lambda v: write_u32(c_uint32(v).value))
_uint32 = (_sized(4, read_u32), write_u32)
_utf16 = (lambda d: d.decode("utf-16-le"), lambda v: v.encode("utf-16-le"))
_text = (lambda d: d.decode(GAME_ENCODING), lambda v: v.encode(GAME_ENCODING, errors="replace"))

ROOM_INFO_FIELDS = {
  2: ("group_id", *_int32),
  3: ("lobby_sv_id", *_int32),
  4: ("host_ip", lambda d: H5_Serializer.read_ipv4(d, True), H5_Serializer.write_ipv4),
  6: ("host_logic_init", *_bool),
  7: ("group_name", *_utf16),
  8: ("password", *_utf16),
  9: ("is_pwd_protected", *_bool),
  10: ("ghost_mode", *_bool),
  11: ("quick_combat", *_bool),
  12: ("fast_combat_turns", *_bool),
  13: ("time_limit", *_int32),
  14: ("difficulty_id", *_uint32),
  15: ("map_desc", H5_RoomInfo.read_nested_string, H5_RoomInfo.write_nested_string),
  19: ("teams", H5_RoomInfo.read_uints, H5_RoomInfo.write_uints),
  20: ("max_players", *_uint32),
  21: ("map_size", *_uint32),
  22: ("player_infos", H5_RoomInfo.read_player_infos, H5_RoomInfo.write_player_infos),
  23: ("game_version", *_uint32),
  24: ("is_saved", *_bool),
  25: ("guid", _sized(16, _read_guid), lambda v: UUID(v).bytes),
  26: ("ubi_send_results_wait", *_bool),
  27: ("options", H5_RoomInfo.read_options, H5_RoomInfo.write_options),
  28: ("is_arena", *_bool),
  29: ("host_checksum", *_uint32),
  30: ("adventure_type", *_uint32),
  31: ("map_desc_tag", H5_RoomInfo.read_nested_string, H5_RoomInfo.write_nested_string),
  32: ("map_goal", *_text),
  33: ("arena_map_name", *_text),
  37: ("int104", *_uint32),
  38: ("int108", *_uint32),
  39: ("combat_turn_speed", *_uint32),
  40: ("flag110", *_bool),
}
"""The room info fields this server knows (Heroes V 2.1's): (attribute, read, write) by field id."""
