import os
import struct
import bz2
import re
from typing import Dict, List, Tuple, Generator, Optional

def js_shift_left(val: int, shift: int) -> int:
    """Simulates JavaScript 32-bit signed left shift (<<) behavior."""
    res = (val << shift) & 0xFFFFFFFF
    if res & 0x80000000:
        res = res - 0x100000000
    return res

def load_packed_list(buf: bytes) -> List[int]:
    """
    Decodes an Accelon packed list of varints from bytes.
    Matches the exact do-while/next loop semantics of JavaScript's loadPackedList.
    """
    if len(buf) < 4:
        return []
    
    added = struct.unpack("<I", buf[0:4])[0]
    offset = 4
    n_milestone = added // 512
    offset += n_milestone * 8
    
    def get_uint8(off: int) -> int:
        if off < len(buf):
            return buf[off]
        return 0
        
    out = []
    remain = added
    v = 0
    
    while remain > 0:
        delta = 0
        shift = 0
        c2 = get_uint8(offset)
        while True:
            delta_term = js_shift_left(c2 & 127, shift)
            delta += delta_term
            delta = (delta & 0xFFFFFFFF)
            if delta & 0x80000000:
                delta = delta - 0x100000000
                
            shift += 7
            offset += 1
            remain -= 1
            if remain > 0:
                c2 = get_uint8(offset)
            else:
                break
            if not (c2 >= 128 and remain > 0):
                break
                
        v = (v + delta) & 0xFFFFFFFF
        if v & 0x80000000:
            v = v - 0x100000000
        out.append(v)
        
    return out


class ROMBlocks:
    """
    Represents a read-only block directory structure used in the Accelon search engine.
    Allows accessing named or indexed sub-blocks of data.
    """
    def __init__(self, buf: bytes, offset: int = 0):
        self.buf = buf
        self.offset = offset
        
        if len(buf) < 16:
            raise ValueError("Buffer too small to parse ROMBlocks header")
            
        feature, signature, totalblocksize, blocksize = struct.unpack("<4I", buf[0:16])
        self.feature = feature
        self.signature = signature
        self.totalblocksize = totalblocksize
        self.blocksize = blocksize
        
        if blocksize != 0:
            count = totalblocksize // blocksize
            lengths = [blocksize * i for i in range(1, count + 1)]
        else:
            # blocksize is 0, so lengths are loaded from a packed list
            adv = struct.unpack("<I", buf[16 + totalblocksize : 20 + totalblocksize])[0]
            lengths = load_packed_list(buf[16 + totalblocksize:])
            count = len(lengths)
            
        self.lengths = lengths
        self.count = count
        
        self.pointers = 0
        if feature & 1073741824:  # boWithPointer
            # adv is the 'added' value of the packed list, which is the byte count of it
            self.pointers = offset + totalblocksize + adv
            
        offset_in_file = offset + 16
        names = []
        
        if (feature & 268435456) and lengths:  # boNamed
            nameblockoffset = lengths[-2]
            namebuf = buf[16 + nameblockoffset:]
            namesblock = ROMBlocks(namebuf, offset_in_file + nameblockoffset)
            off = 0
            for i in range(namesblock.count):
                if i > 0:
                    length = namesblock.lengths[i] - namesblock.lengths[i-1]
                else:
                    length = namesblock.lengths[i]
                
                # Decoded names are in UTF-16LE, excluding the trailing null terminator (2 bytes)
                name_bytes = namebuf[off + 16 : off + 16 + length - 2]
                name_str = name_bytes.decode("utf-16-le", errors="ignore").rstrip("\x00")
                off = namesblock.lengths[i]
                names.append(name_str)
            self.count -= 1
            
        self.names = names
        self.offset = offset_in_file

    def get_raw_data(self, i: int) -> bytes:
        """Returns the raw bytes of the i-th block."""
        if i == 0:
            start = 16
        else:
            start = self.lengths[i-1] + 16
        end = self.lengths[i] + 16
        return self.buf[start:end]
        
    def get_block_offset(self, name_or_idx) -> int:
        """Gets the file offset of a block by name or index."""
        if isinstance(name_or_idx, str):
            if name_or_idx not in self.names:
                raise ValueError(f"Wrong block name: {name_or_idx}")
            idx = self.names.index(name_or_idx)
        else:
            idx = name_or_idx
            
        if idx == 0:
            return self.offset
        else:
            return self.offset + self.lengths[idx-1]


class AccelonDB:
    """
    Main parser class for Accelon Database (.adb) dictionary/encyclopedia files.
    """
    def __init__(self, adb_path: str):
        self.adb_path = adb_path
        if not os.path.exists(adb_path):
            raise FileNotFoundError(f"Database file not found: {adb_path}")
            
        with open(adb_path, "rb") as f:
            self.buf = f.read()
            
        self._parse_header()
        self._parse_blocks()
        
    def _parse_header(self):
        """Parses the 256-byte header of the Accelon database."""
        header_buf = self.buf[0:256]
        if len(header_buf) < 256:
            raise ValueError("Invalid file: too small to contain ADB header")
            
        self.dbname = header_buf[0:32].decode("ascii", errors="ignore").rstrip("\x00")
        self.signature = header_buf[32:80].decode("ascii", errors="ignore").rstrip("\x00")
        
        expected_sig = "\r\nAccelon Search Engine\r\ndesigned by C.S.Yap"
        if self.signature != expected_sig:
            raise ValueError("Invalid Accelon database signature")
            
        # Unpack the 12 little-endian int32 parameters at offset 80
        ints = struct.unpack("<12i", header_buf[80:128])
        self.textterminator = ints[0]
        self.version = ints[1]
        self.compression = ints[2]
        self.srcblocksize = ints[3]
        self.maxblockoffset = ints[4]
        self.andtagid = ints[5]
        self.linecount = ints[6]
        self.dbtype = ints[7]
        self.protection = ints[8]
        self.tagcount = ints[9]
        self.features = ints[10]
        self.reserved2 = ints[11]
        
        self.dbcname = header_buf[128:192].decode("utf-16-le", errors="ignore").rstrip("\x00")
        self.serial = header_buf[192:208].decode("ascii", errors="ignore").rstrip("\x00")
        self.pw = header_buf[208:228].decode("ascii", errors="ignore").rstrip("\x00")
        
        self.crc32 = struct.unpack("<I", header_buf[251:255])[0]
        self.tokencount = self.maxblockoffset - self.tagcount

    def _parse_blocks(self):
        """Parses sub-blocks container and nested directory tables."""
        # Top-level blocks directory starts at offset 256
        self.blocks = ROMBlocks(self.buf[256:], 256)
        
        # Parse nested blocks (source, resources, tables)
        if "source" in self.blocks.names:
            source_off = self.blocks.get_block_offset("source")
            self.sources = ROMBlocks(self.buf[source_off:], source_off)
        else:
            self.sources = None
            
        if "resources" in self.blocks.names:
            res_off = self.blocks.get_block_offset("resources")
            self.resources = ROMBlocks(self.buf[res_off:], res_off)
        else:
            self.resources = None
            
        if "tables" in self.blocks.names:
            tables_off = self.blocks.get_block_offset("tables")
            self.tables = ROMBlocks(self.buf[tables_off:], tables_off)
            
            if "lines.physical" in self.tables.names:
                lines_off = self.tables.get_block_offset("lines.physical")
                self.PALines = load_packed_list(self.buf[lines_off:])
            else:
                self.PALines = []
        else:
            self.tables = None
            self.PALines = []

    def get_text_block(self, i: int) -> str:
        """Decompresses and returns the i-th text block from source."""
        if not self.sources:
            return ""
        raw_data = self.sources.get_raw_data(i)
        
        # Decompress using bz2
        decompressed = bz2.decompress(raw_data)
        
        # Decode as UTF-16LE
        return decompressed.decode("utf-16-le", errors="ignore")

    def get_xml(self) -> str:
        """Reconstructs the full XML database using source text and PALines indexing."""
        if not self.sources or not self.PALines:
            return ""
            
        print("Decompressing database text blocks...")
        raw_text_blocks = []
        for i in range(self.sources.count):
            raw_text_blocks.append(self.get_text_block(i))
            
        all_text = "".join(raw_text_blocks)
        
        print("Slicing into physical lines...")
        out_lines = []
        for i in range(len(self.PALines)):
            start = (0 if i == 0 else self.PALines[i - 1]) >> 1
            end = self.PALines[i] >> 1
            out_lines.append(all_text[start:end])
            
        return "\n".join(out_lines)

    def get_xml_files(self) -> Dict[str, bytes]:
        """
        Splits the full database XML into separate documents based on `<檔` tags.
        Matches the breakxml JS logic.
        """
        full_xml = self.get_xml()
        files = {}
        
        prev = 0
        at = full_xml.find("<\u6A94")  # "<\u6A94" is "<檔"
        prev_name = f"{self.dbname}.xml"
        count = 0
        
        # Pattern to extract document name: n="filename"
        name_pattern = re.compile(r'n="(.+?)"')
        
        while at >= 0:
            tag_line = full_xml[at : at + 200]
            at2 = tag_line.find('">')
            if at2 != -1:
                tag_line = tag_line[: at2 + 2]
                
            name_match = name_pattern.search(tag_line)
            if not name_match:
                name = f"{self.dbname}.{count}.xml"
            else:
                name = name_match.group(1)
                
            if at > prev:
                files[prev_name] = full_xml[prev:at].encode("utf-8")
                
            count += 1
            prev = at
            prev_name = name
            at = full_xml.find("<\u6A94", prev + 3)
            
        # Remaining content
        files[prev_name] = full_xml[prev:].encode("utf-8")
        
        # If multiple files were generated, also create a list file (.lst)
        if len(files) > 1:
            list_content = "\n".join(files.keys()).encode("utf-8")
            files[f"{self.dbname}.lst"] = list_content
            
        return files

    def get_resources(self) -> Dict[str, bytes]:
        """Extracts and returns all resources (e.g. PNG files) in the database."""
        resources_dict = {}
        if not self.resources:
            return resources_dict
            
        for i in range(self.resources.count):
            name = self.resources.names[i]
            content = self.resources.get_raw_data(i)
            resources_dict[name] = content
            
        return resources_dict

    def extract_all(self, output_dir: str):
        """Extracts all XML documents and resource files into the specified directory."""
        os.makedirs(output_dir, exist_ok=True)
        
        # 1. Extract XML files
        xml_files = self.get_xml_files()
        print(f"Saving {len(xml_files)} XML/text files to {output_dir}...")
        for name, content in xml_files.items():
            path = os.path.join(output_dir, name)
            # Create subdirectories if name contains path separators
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(content)
                
        # 2. Extract resources
        resources = self.get_resources()
        if resources:
            print(f"Saving {len(resources)} resource files to {output_dir}...")
            for name, content in resources.items():
                path = os.path.join(output_dir, name)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as f:
                    f.write(content)
                    
        print(f"Extraction completed successfully in: {output_dir}")
