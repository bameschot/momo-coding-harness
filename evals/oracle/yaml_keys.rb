# Independent YAML oracle: Ruby's Psych parser (libyaml).  For every file named
# in ARGV[0] prints "F\t<path>" and one "K\t<key path>\t<line>" per mapping key,
# using the index's conventions: dotted paths, "[]" for sequence items, keys
# nested at most 6 mappings deep (code_nav._MAX_DATA_DEPTH).
require 'yaml'

MAX_DEPTH = 6

def walk(node, prefix, depth, out)
  case node
  when Psych::Nodes::Mapping
    return if depth >= MAX_DEPTH
    node.children.each_slice(2) do |k, v|
      next unless k.is_a?(Psych::Nodes::Scalar)
      path = prefix.empty? ? k.value : "#{prefix}.#{k.value}"
      out << "K\t#{path}\t#{k.start_line + 1}"
      walk(v, path, depth + 1, out)
    end
  when Psych::Nodes::Sequence
    node.children.each { |c| walk(c, prefix.empty? ? "[]" : "#{prefix}[]", depth, out) }
  when Psych::Nodes::Document
    node.children.each { |c| walk(c, prefix, depth, out) }
  end
end

File.readlines(ARGV[0], chomp: true).each do |path|
  out = ["F\t#{path}"]
  begin
    Psych.parse_stream(File.read(path)).children.each { |doc| walk(doc, "", 0, out) }
  rescue Psych::SyntaxError, ArgumentError => e
    out = ["F\t#{path}", "E\t#{e.class}"]
  end
  puts out
end
