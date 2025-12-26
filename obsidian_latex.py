#!/usr/bin/env python3
"""
Obsidian to LaTeX Compiler
Converts Obsidian markdown notes to LaTeX/PDF with embed resolution
"""

import re
import sys
import os
import shutil
import subprocess
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Set, Optional, Tuple
import yaml


@dataclass
class CompilationStats:
    """Track compilation statistics and warnings"""
    files_embedded: Set[str] = field(default_factory=set)
    sections_processed: int = 0
    images_copied: Set[str] = field(default_factory=set)
    internal_links: int = 0
    warnings: List[str] = field(default_factory=list)
    
    def add_warning(self, message: str):
        self.warnings.append(message)
    
    def print_summary(self, output_pdf: Path):
        print(f"\n✓ Compilation complete: {output_pdf}")
        print(f"\nStatistics:")
        print(f"- {len(self.files_embedded)} files embedded")
        print(f"- {self.sections_processed} sections processed")
        print(f"- {len(self.images_copied)} images copied")
        print(f"- {self.internal_links} internal links resolved")
        
        if self.warnings:
            print(f"\nWarnings:")
            for warning in self.warnings:
                print(f"⚠ {warning}")


@dataclass
class Config:
    """Configuration for compilation"""
    vault_path: Path
    template_dir: Path
    vault_name: str = "MyVault"
    attachments_folder: str = "attachments"
    default_template: str = "default"
    obsidian_uri: bool = True
    output_base_dir: Optional[Path] = None
    latex_engine: str = "pdflatex"
    compile_twice: bool = True
    keep_aux_files: bool = False
    create_zip: bool = True
    open_pdf_after_compile: bool = False


class ObsidianParser:
    """Parse Obsidian markdown and handle embeds"""
    
    def __init__(self, vault_path: Path, stats: CompilationStats):
        self.vault_path = vault_path
        self.stats = stats
        self.embedded_files: Set[str] = set()
        
    def read_file(self, filepath: Path) -> Tuple[Dict, str]:
        """Read file and extract YAML frontmatter"""
        if not filepath.exists():
            self.stats.add_warning(f"File not found: {filepath}")
            return {}, ""
        
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Extract YAML frontmatter
        frontmatter = {}
        if content.startswith('---\n'):
            try:
                end = content.index('\n---\n', 4)
                frontmatter = yaml.safe_load(content[4:end])
                content = content[end + 5:]
            except (ValueError, yaml.YAMLError):
                pass
        
        return frontmatter, content
    
    def extract_section(self, content: str, section_name: str) -> Optional[str]:
        """Extract a specific section from markdown content"""
        lines = content.split('\n')
        in_section = False
        section_level = None
        result_lines = []
        
        for line in lines:
            # Check if this is a heading
            heading_match = re.match(r'^(#{1,6})\s+(.+)$', line)
            
            if heading_match:
                level = len(heading_match.group(1))
                title = heading_match.group(2).strip()
                
                # Check if this is our target section
                if title == section_name and not in_section:
                    in_section = True
                    section_level = level
                    continue  # Skip the heading itself as per requirements
                
                # If we're in the section and hit a same/higher level heading, we're done
                elif in_section and level <= section_level:
                    break
            
            if in_section:
                result_lines.append(line)
        
        if not in_section:
            self.stats.add_warning(f"Section '{section_name}' not found")
            return None
        
        return '\n'.join(result_lines)
    
    def demote_headings(self, content: str, levels: int = 1) -> str:
        """Demote all headings by N levels"""
        def replace_heading(match):
            current_hashes = match.group(1)
            title = match.group(2)
            new_hashes = '#' * min(len(current_hashes) + levels, 6)
            return f"{new_hashes} {title}"
        
        return re.sub(r'^(#{1,6})\s+(.+)$', replace_heading, content, flags=re.MULTILINE)
    
    def resolve_embed(self, embed_match: str, current_heading_level: int, line_num: int) -> str:
        """Resolve an embed like ![[file]] or ![[file#section]]"""
        # Parse embed syntax: ![[filename]] or ![[filename#section]]
        match = re.match(r'!\[\[([^\]#]+)(?:#([^\]]+))?\]\]', embed_match)
        if not match:
            return embed_match
        
        filename = match.group(1).strip()
        section = match.group(2).strip() if match.group(2) else None
        
        # Check for duplicates
        embed_key = f"{filename}#{section}" if section else filename
        if embed_key in self.embedded_files:
            self.stats.add_warning(f"Duplicate embed skipped: {embed_key} (line {line_num})")
            return ""
        
        self.embedded_files.add(embed_key)
        self.stats.files_embedded.add(filename)
        
        # Find the file - search recursively through vault
        filepath = self._find_file(filename)
        
        if filepath is None:
            self.stats.add_warning(f"Missing file: {filename}.md (line {line_num})")
            return f"[MISSING: {filename}]"
        
        # Read the file
        _, content = self.read_file(filepath)
        
        # Extract section if specified
        if section:
            content = self.extract_section(content, section)
            if content is None:
                return f"[MISSING SECTION: {filename}#{section}]"
        
        # Demote headings by one level relative to current position
        content = self.demote_headings(content, levels=1)
        
        self.stats.sections_processed += 1
        
        return content
    
    def _find_file(self, filename: str) -> Optional[Path]:
        """Find a markdown file by name, searching recursively through vault"""
        # Try exact match in root first (fastest)
        direct_path = self.vault_path / f"{filename}.md"
        if direct_path.exists():
            return direct_path
        
        # Search recursively through vault
        for md_file in self.vault_path.rglob(f"{filename}.md"):
            return md_file
        
        return None
    
    def get_current_heading_level(self, lines: List[str], current_idx: int) -> int:
        """Get the heading level of the most recent heading before current line"""
        for i in range(current_idx - 1, -1, -1):
            match = re.match(r'^(#{1,6})\s+', lines[i])
            if match:
                return len(match.group(1))
        return 0
    
    def process_embeds(self, content: str) -> str:
        """Process all embeds in content"""
        lines = content.split('\n')
        result_lines = []
        
        for line_num, line in enumerate(lines, 1):
            # Find embeds in this line
            embed_pattern = r'!\[\[[^\]]+\]\]'
            
            if re.search(embed_pattern, line):
                current_level = self.get_current_heading_level(lines, line_num)
                
                # Replace all embeds in the line
                def replace_embed(match):
                    return self.resolve_embed(match.group(0), current_level, line_num)
                
                line = re.sub(embed_pattern, replace_embed, line)
            
            result_lines.append(line)
        
        return '\n'.join(result_lines)


class LatexConverter:
    """Convert markdown to LaTeX"""
    
    def __init__(self, vault_path: Path, output_dir: Path, stats: CompilationStats, config: Config):
        self.vault_path = vault_path
        self.output_dir = output_dir
        self.stats = stats
        self.config = config
        self.label_counter = 0
        self.section_labels: Dict[str, str] = {}
        
    def convert_wikilinks(self, content: str) -> str:
        """Convert [[links]] to LaTeX hyperlinks or internal refs"""
        def replace_link(match):
            full_match = match.group(0)
            filename = match.group(1).strip()
            section = match.group(2).strip() if match.group(2) else None
            display = match.group(3).strip() if match.group(3) else None
            
            # Determine display text
            if display:
                link_text = display
            elif section:
                link_text = section
            else:
                link_text = filename
            
            # Check if this file is embedded (internal reference)
            embed_key = f"{filename}#{section}" if section else filename
            if embed_key in self.stats.files_embedded or filename in self.stats.files_embedded:
                # Internal reference
                self.stats.internal_links += 1
                label = self.section_labels.get(embed_key, f"sec:{filename.replace(' ', '_')}")
                return f"\\hyperref[{label}]{{{link_text}}}"
            else:
                # External link to Obsidian
                if self.config.obsidian_uri:
                    file_part = filename.replace(' ', '%20')
                    uri = f"obsidian://open?vault={self.config.vault_name}&file={file_part}"
                    if section:
                        uri += f"&heading={section.replace(' ', '%20')}"
                    return f"\\href{{{uri}}}{{{link_text}}}"
                else:
                    # File path fallback
                    filepath = self.vault_path / f"{filename}.md"
                    return f"\\href{{file://{filepath}}}{{{link_text}}}"
        
        # Pattern: [[filename]] or [[filename#section]] or [[filename|display]] or [[filename#section|display]]
        pattern = r'\[\[([^\]#|]+)(?:#([^\]|]+))?(?:\|([^\]]+))?\]\]'
        return re.sub(pattern, replace_link, content)
    
    def convert_images(self, content: str) -> str:
        """Convert image embeds and copy images to output"""
        def replace_image(match):
            filename = match.group(1).strip()
            
            # Find image in attachments folder
            image_path = self.vault_path / self.config.attachments_folder / filename
            
            if not image_path.exists():
                self.stats.add_warning(f"Image not found: {filename}")
                return f"[MISSING IMAGE: {filename}]"
            
            # Copy to output directory
            output_images = self.output_dir / "images"
            output_images.mkdir(exist_ok=True)
            
            output_path = output_images / filename
            shutil.copy2(image_path, output_path)
            self.stats.images_copied.add(filename)
            
            # Return LaTeX image include with relative path
            return f"\\includegraphics[max width=\\textwidth]{{images/{filename}}}"
        
        # Pattern: ![[image.png]] or ![alt](image.png)
        content = re.sub(r'!\[\[([^\]]+\.(png|jpg|jpeg|gif|pdf))\]\]', replace_image, content, flags=re.IGNORECASE)
        content = re.sub(r'!\[([^\]]*)\]\(([^)]+\.(png|jpg|jpeg|gif|pdf))\)', 
                        lambda m: replace_image(re.match(r'(.+)', m.group(2))), content, flags=re.IGNORECASE)
        
        return content
    
    def convert_footnotes(self, content: str) -> str:
        """Convert markdown footnotes to LaTeX"""
        # Inline footnotes: ^[text]
        content = re.sub(r'\^\[([^\]]+)\]', r'\\footnote{\1}', content)
        
        # Standard markdown footnotes: [^1] and [^1]: definition
        footnote_defs = {}
        
        # Extract definitions
        def extract_def(match):
            key = match.group(1)
            text = match.group(2)
            footnote_defs[key] = text
            return ""  # Remove from content
        
        content = re.sub(r'^\[\^(\w+)\]:\s*(.+)$', extract_def, content, flags=re.MULTILINE)
        
        # Replace references
        def replace_ref(match):
            key = match.group(1)
            if key in footnote_defs:
                return f"\\footnote{{{footnote_defs[key]}}}"
            return match.group(0)
        
        content = re.sub(r'\[\^(\w+)\]', replace_ref, content)
        
        return content
    
    def convert_tables(self, content: str) -> str:
        """Convert markdown tables to simple LaTeX tables"""
        lines = content.split('\n')
        result = []
        in_table = False
        table_lines = []
        
        for line in lines:
            # Check if line is part of a table
            if '|' in line and line.strip().startswith('|'):
                if not in_table:
                    in_table = True
                    table_lines = []
                table_lines.append(line)
            else:
                if in_table:
                    # End of table, convert it
                    result.append(self._convert_table(table_lines))
                    in_table = False
                    table_lines = []
                result.append(line)
        
        # Handle table at end of content
        if in_table:
            result.append(self._convert_table(table_lines))
        
        return '\n'.join(result)
    
    def _convert_table(self, table_lines: List[str]) -> str:
        """Convert a markdown table to LaTeX tabular"""
        if len(table_lines) < 2:
            return '\n'.join(table_lines)
        
        # Parse header
        header = [cell.strip() for cell in table_lines[0].split('|')[1:-1]]
        num_cols = len(header)
        
        # Skip separator line (table_lines[1])
        
        # Parse data rows
        data_rows = []
        for line in table_lines[2:]:
            cells = [cell.strip() for cell in line.split('|')[1:-1]]
            if len(cells) == num_cols:
                data_rows.append(cells)
        
        # Generate LaTeX
        col_spec = 'l' * num_cols
        latex = f"\\begin{{tabular}}{{{col_spec}}}\n"
        latex += "\\hline\n"
        latex += " & ".join(header) + " \\\\\n"
        latex += "\\hline\n"
        
        for row in data_rows:
            latex += " & ".join(row) + " \\\\\n"
        
        latex += "\\hline\n"
        latex += "\\end{tabular}"
        
        return latex
    
    def convert_markdown_to_latex(self, content: str) -> str:
        """Main conversion function"""
        # Convert footnotes first
        content = self.convert_footnotes(content)
        
        # Convert tables
        content = self.convert_tables(content)
        
        # Convert images
        content = self.convert_images(content)
        
        # Convert wikilinks
        content = self.convert_wikilinks(content)
        
        # Convert headings (# to \section, ## to \subsection, etc.)
        heading_map = {
            1: 'section',
            2: 'subsection',
            3: 'subsubsection',
            4: 'paragraph',
            5: 'subparagraph',
            6: 'subparagraph'
        }
        
        def replace_heading(match):
            level = len(match.group(1))
            title = match.group(2)
            cmd = heading_map.get(level, 'paragraph')
            
            # Generate label for cross-references
            label = f"sec:{title.lower().replace(' ', '_')}"
            self.label_counter += 1
            
            return f"\\{cmd}{{{title}}}\\label{{{label}}}"
        
        content = re.sub(r'^(#{1,6})\s+(.+)$', replace_heading, content, flags=re.MULTILINE)
        
        # Convert lists
        content = self._convert_lists(content)
        
        # Convert emphasis
        content = re.sub(r'\*\*(.+?)\*\*', r'\\textbf{\1}', content)
        content = re.sub(r'\*(.+?)\*', r'\\textit{\1}', content)
        content = re.sub(r'__(.+?)__', r'\\textbf{\1}', content)
        content = re.sub(r'_(.+?)_', r'\\textit{\1}', content)
        
        # Convert code
        content = re.sub(r'`([^`]+)`', r'\\texttt{\1}', content)
        
        # Handle code blocks
        content = self._convert_code_blocks(content)
        
        return content
    
    def _convert_lists(self, content: str) -> str:
        """Convert markdown lists to LaTeX itemize/enumerate"""
        lines = content.split('\n')
        result = []
        in_list = False
        list_type = None
        
        for line in lines:
            # Check for unordered list
            unordered = re.match(r'^(\s*)[-*+]\s+(.+)$', line)
            # Check for ordered list
            ordered = re.match(r'^(\s*)\d+\.\s+(.+)$', line)
            
            if unordered or ordered:
                item_text = unordered.group(2) if unordered else ordered.group(2)
                current_type = 'itemize' if unordered else 'enumerate'
                
                if not in_list:
                    result.append(f"\\begin{{{current_type}}}")
                    in_list = True
                    list_type = current_type
                elif list_type != current_type:
                    result.append(f"\\end{{{list_type}}}")
                    result.append(f"\\begin{{{current_type}}}")
                    list_type = current_type
                
                result.append(f"  \\item {item_text}")
            else:
                if in_list:
                    result.append(f"\\end{{{list_type}}}")
                    in_list = False
                    list_type = None
                result.append(line)
        
        if in_list:
            result.append(f"\\end{{{list_type}}}")
        
        return '\n'.join(result)
    
    def _convert_code_blocks(self, content: str) -> str:
        """Convert fenced code blocks"""
        def replace_code(match):
            lang = match.group(1) or ''
            code = match.group(2)
            return f"\\begin{{verbatim}}\n{code}\n\\end{{verbatim}}"
        
        return re.sub(r'```(\w*)\n(.*?)```', replace_code, content, flags=re.DOTALL)


def load_config(config_path: Optional[Path] = None) -> Config:
    """Load configuration from file in script directory"""
    script_dir = Path(__file__).parent
    
    if config_path is None:
        config_path = script_dir / "config.yaml"
    
    if not config_path.exists():
        print(f"Error: config.yaml not found at {config_path}")
        print("\nPlease create a config.yaml file with:")
        print("vault_path: /path/to/your/obsidian/vault")
        print("template_dir: /path/to/templates")
        sys.exit(1)
    
    with open(config_path, 'r') as f:
        data = yaml.safe_load(f) or {}
    
    # Convert paths to Path objects
    if 'vault_path' not in data:
        print("Error: vault_path not specified in config.yaml")
        sys.exit(1)
    
    data['vault_path'] = Path(data['vault_path']).expanduser()
    
    if 'template_dir' not in data:
        print("Error: template_dir not specified in config.yaml")
        sys.exit(1)
    
    data['template_dir'] = Path(data['template_dir']).expanduser()
    
    if 'output_base_dir' in data and data['output_base_dir']:
        data['output_base_dir'] = Path(data['output_base_dir']).expanduser()
    
    return Config(**data)


def load_template(template_name: str, config: Config) -> str:
    """Load LaTeX template from file"""
    template_path = config.template_dir / f"{template_name}.tex"
    
    if not template_path.exists():
        print(f"Error: Template {template_name}.tex not found in {config.template_dir}")
        sys.exit(1)
    
    with open(template_path, 'r', encoding='utf-8') as f:
        return f.read()


def load_custom_macros(config: Config) -> str:
    """Load custom LaTeX macros if they exist"""
    macros_path = config.template_dir / "macros.tex"
    
    if macros_path.exists():
        with open(macros_path, 'r', encoding='utf-8') as f:
            return f.read()
    
    return ""


def create_latex_document(frontmatter: Dict, body: str, template_name: str, config: Config) -> str:
    """Create complete LaTeX document from template"""
    # Load template
    template = load_template(template_name, config)
    
    # Load custom macros
    custom_macros = load_custom_macros(config)
    
    # Extract metadata with defaults
    title = frontmatter.get('title', 'Document')
    author = frontmatter.get('author', '')
    date = frontmatter.get('date', '\\today')

    # Convert to strings in case YAML parsed them as other types
    title = str(title) if title else 'Document'
    author = str(author) if author else ''
    date = str(date) if date and date != '\\today' else '\\today'
    
    # Replace placeholders
    template = template.replace('{{title}}', title)
    template = template.replace('{{author}}', author)
    template = template.replace('{{date}}', date)
    template = template.replace('{{custom_macros}}', custom_macros)
    template = template.replace('{{body}}', body)
    
    return template


def compile_latex(tex_file: Path, output_dir: Path, config: Config) -> bool:
    """Compile LaTeX to PDF"""
    try:
        # Run latex engine (pdflatex/xelatex) once or twice
        runs = 2 if config.compile_twice else 1
        
        for _ in range(runs):
            result = subprocess.run(
                [config.latex_engine, '-interaction=nonstopmode', '-output-directory', 
                 str(output_dir), str(tex_file)],
                capture_output=True,
                text=True
            )
            if result.returncode != 0:
                print(f"LaTeX compilation error:\n{result.stdout}")
                return False
        
        # Clean up auxiliary files if configured
        if not config.keep_aux_files:
            for ext in ['.aux', '.log', '.out']:
                aux_file = output_dir / f"main{ext}"
                if aux_file.exists():
                    aux_file.unlink()
        
        return True
    except FileNotFoundError:
        print(f"Error: {config.latex_engine} not found. Please install a LaTeX distribution.")
        return False


def main():
    if len(sys.argv) < 2:
        print("Usage: python compile.py <master_file.md> [options]")
        print("\nOptions:")
        print("  --output <dir>      Output directory")
        print("  --template <name>   Template name (default: from config)")
        print("  --config <file>     Config file path (default: config.yaml in script dir)")
        print("  --watch            Watch mode (recompile on changes)")
        print("  --interval <sec>   Watch interval in seconds (default: 1)")
        sys.exit(1)
    
    master_file = Path(sys.argv[1])
    if not master_file.exists():
        print(f"Error: {master_file} not found")
        sys.exit(1)
    
    # Parse arguments
    output_dir = None
    template_name = None
    config_path = None
    watch_mode = False
    watch_interval = 1
    
    i = 2
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg == '--output' and i + 1 < len(sys.argv):
            output_dir = Path(sys.argv[i + 1])
            i += 2
        elif arg == '--template' and i + 1 < len(sys.argv):
            template_name = sys.argv[i + 1]
            i += 2
        elif arg == '--config' and i + 1 < len(sys.argv):
            config_path = Path(sys.argv[i + 1])
            i += 2
        elif arg == '--watch':
            watch_mode = True
            i += 1
        elif arg == '--interval' and i + 1 < len(sys.argv):
            watch_interval = int(sys.argv[i + 1])
            i += 2
        else:
            i += 1
    
    # Load configuration
    config = load_config(config_path)
    
    # Setup output directory
    if output_dir is None:
        if config.output_base_dir:
            output_dir = config.output_base_dir / f"{master_file.stem}_output"
        else:
            output_dir = master_file.parent / f"{master_file.stem}_output"
    
    # Use template from config if not specified
    if template_name is None:
        template_name = config.default_template
    
    def compile_document():
        """Compilation function that can be called repeatedly in watch mode"""
        output_dir.mkdir(exist_ok=True)
        
        stats = CompilationStats()
        
        print(f"\n{'='*60}")
        print(f"Compiling {master_file.name}...")
        print(f"Template: {template_name}")
        print(f"{'='*60}\n")
        
        # Parse master file
        parser = ObsidianParser(config.vault_path, stats)
        frontmatter, content = parser.read_file(master_file)
        
        # Process embeds
        content = parser.process_embeds(content)
        
        # Convert to LaTeX
        converter = LatexConverter(config.vault_path, output_dir, stats, config)
        latex_body = converter.convert_markdown_to_latex(content)
        
        # Create full document
        latex_doc = create_latex_document(frontmatter, latex_body, template_name, config)
        
        # Write LaTeX file
        tex_file = output_dir / "main.tex"
        with open(tex_file, 'w', encoding='utf-8') as f:
            f.write(latex_doc)
        
        print(f"✓ LaTeX file written: {tex_file}")
        
        # Compile to PDF
        print("Compiling LaTeX to PDF...")
        if compile_latex(tex_file, output_dir, config):
            pdf_file = output_dir / "main.pdf"
            
            # Create zip file if configured
            if config.create_zip:
                zip_path = output_dir.parent / f"{master_file.stem}_output"
                shutil.make_archive(str(zip_path), 'zip', output_dir)
                print(f"✓ Zip archive: {zip_path}.zip")
            
            stats.print_summary(pdf_file)
            print(f"\nOutput folder: {output_dir}")
            
            return True
        else:
            print("✗ PDF compilation failed")
            return False
    
    if watch_mode:
        print(f"Watch mode enabled (checking every {watch_interval}s)")
        print("Press Ctrl+C to stop\n")
        
        last_modified = master_file.stat().st_mtime
        
        # Initial compilation
        compile_document()
        
        try:
            while True:
                time.sleep(watch_interval)
                current_modified = master_file.stat().st_mtime
                
                if current_modified > last_modified:
                    print(f"\n[{time.strftime('%H:%M:%S')}] Change detected, recompiling...")
                    compile_document()
                    last_modified = current_modified
        except KeyboardInterrupt:
            print("\n\nWatch mode stopped.")
    else:
        success = compile_document()
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()