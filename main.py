import sys
import os
import argparse
import re
from code_base.accelon import AccelonDB

def show_info(db: AccelonDB):
    """Prints metadata and structure of the Accelon database."""
    print("==================================================")
    print("             ACCELON DATABASE INFO                ")
    print("==================================================")
    print(f"File Path:       {db.adb_path}")
    print(f"DB Name:         {db.dbname}")
    print(f"DBC Name (CN):   {db.dbcname}")
    print(f"Version:         {db.version}")
    print(f"Line Count:      {db.linecount:,}")
    print(f"Signature:       {repr(db.signature)}")
    print(f"Compression:     {db.compression}")
    print(f"Src Block Size:  {db.srcblocksize}")
    print(f"Tag Count:       {db.tagcount}")
    print(f"Token Count:     {db.tokencount}")
    print(f"Serial:          {db.serial if db.serial else 'N/A'}")
    print(f"Password Protected: {'Yes' if db.pw else 'No'}")
    print("--------------------------------------------------")
    
    if db.sources:
        print(f"Text Blocks ('source'):     {db.sources.count:,} blocks")
    else:
        print("Text Blocks ('source'):     None")
        
    if db.resources:
        print(f"Resource Files ('resources'): {db.resources.count:,} files (e.g. images)")
    else:
        print("Resource Files ('resources'): None")
        
    if db.tables:
        print(f"Tables ('tables'):           {', '.join(db.tables.names)}")
    else:
        print("Tables ('tables'):           None")
    print("==================================================")

def do_search(db: AccelonDB, query: str, limit: int = 20, mode: str = "all"):
    """Searches physical lines in the database XML for a query string, with fallback conversion and mode filtering."""
    if not db.sources or not db.PALines:
        print("Error: No source text or line offsets found in the database.")
        return

    print("Decompressing database text blocks...")
    raw_text_blocks = []
    for i in range(db.sources.count):
        raw_text_blocks.append(db.get_text_block(i))
    all_text = "".join(raw_text_blocks)

    # Patterns for filtering
    headword_pattern = re.compile(r'<_*(?:-)*詞[^>]*>(.*?)</_*(?:-)*詞>')
    quote_pattern = re.compile(r'[“‘「『]([^”’」』]+)[”’」』]')

    def strip_xml_tags(text: str) -> str:
        return re.sub(r'<[^>]+>', '', text)

    def search_in_text(search_query: str) -> int:
        # Split search query by whitespace to support AND search
        subqueries = [sq for sq in search_query.split() if sq]
        if not subqueries:
            return 0

        matches = 0
        for i in range(len(db.PALines)):
            start = (0 if i == 0 else db.PALines[i - 1]) >> 1
            end = db.PALines[i] >> 1
            line = all_text[start:end]
            
            is_match = False
            if mode == "all":
                is_match = all(subq in line for subq in subqueries)
            elif mode == "headword":
                headwords = [strip_xml_tags(m.group(1)) for m in headword_pattern.finditer(line)]
                is_match = any(all(subq in hw for subq in subqueries) for hw in headwords)
            elif mode == "example":
                # Remove all headword tags to avoid matching cross-references inside quotes
                line_no_hw = headword_pattern.sub('', line)
                quotes = [strip_xml_tags(m.group(1)) for m in quote_pattern.finditer(line_no_hw)]
                is_match = any(all(subq in q for subq in subqueries) for q in quotes)

            if is_match:
                matches += 1
                print(f"\n[Line {i+1}] {line.strip()}")
                if matches >= limit:
                    print(f"\nShowed {limit} matches. (Total matching count not computed to save memory/time)")
                    break
        return matches

    mode_desc = {
        "all": "entire line",
        "headword": "headwords (조목) only",
        "example": "example sentences (예문) only"
    }[mode]
    print(f"Searching for '{query}' in physical lines ({mode_desc}, showing up to {limit} matches)...")
    matches = search_in_text(query)
    
    if matches == 0:
        try:
            from opencc import OpenCC
            cc = OpenCC('t2s')
            converted_query = cc.convert(query)
        except Exception as e:
            print(f"Error initializing OpenCC or converting query: {e}")
            converted_query = query
            
        if converted_query != query:
            print(f"No matches found for '{query}'. Trying Traditional-to-Simplified conversion: '{converted_query}'...")
            matches = search_in_text(converted_query)
            
    if matches == 0:
        print("No matches found.")
    else:
        print(f"\nFound {matches} match(es).")

def do_extract(db: AccelonDB, output_dir: str):
    """Extracts database text XML and images/resources to output_dir."""
    print(f"Starting extraction to: {os.path.abspath(output_dir)}")
    try:
        db.extract_all(output_dir)
        print("Extraction complete!")
    except Exception as e:
        print(f"Error during extraction: {e}")

def main():
    parser = argparse.ArgumentParser(
        description="Accelon Database (.adb) parser and query utility for Python."
    )
    
    parser.add_argument(
        "--db",
        default="/Users/mt/MT/project/kjj_2/data/hanyu.adb",
        help="Path to the hanyu.adb file (default: data/hanyu.adb)"
    )
    
    subparsers = parser.add_subparsers(dest="command", required=True, help="Sub-commands")
    
    # Info subcommand
    subparsers.add_parser("info", help="Show database metadata and structure")
    
    # Search subcommand
    search_parser = subparsers.add_parser("search", help="Search lines for a query string")
    search_parser.add_argument("query", type=str, help="Search query string")
    search_parser.add_argument("--limit", type=int, default=20, help="Max matches to display (default: 20)")
    search_parser.add_argument(
        "--mode", "-m",
        choices=["all", "headword", "example"],
        default="all",
        help="Search mode: 'all' for entire line, 'headword' for headwords (조목) only, 'example' for example sentences (예문) only (default: all)"
    )
    
    # Extract subcommand
    extract_parser = subparsers.add_parser("extract", help="Extract XML data and resource files")
    extract_parser.add_argument(
        "output_dir", 
        type=str, 
        nargs="?", 
        default="extracted_data", 
        help="Target directory for extracted files (default: extracted_data)"
    )
    
    args = parser.parse_args()
    
    # Check if database file exists
    if not os.path.exists(args.db):
        print(f"Error: Database file not found at: {args.db}")
        sys.exit(1)
        
    print(f"Loading database {args.db}...")
    try:
        db = AccelonDB(args.db)
    except Exception as e:
        print(f"Failed to parse database: {e}")
        sys.exit(1)
        
    if args.command == "info":
        show_info(db)
    elif args.command == "search":
        do_search(db, args.query, args.limit, args.mode)
    elif args.command == "extract":
        do_extract(db, args.output_dir)

if __name__ == "__main__":
    main()
