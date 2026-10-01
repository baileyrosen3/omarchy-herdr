"""Build the README's vector banner from the bundled upstream logo paths."""
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
ET.register_namespace("", "http://www.w3.org/2000/svg")


def logo(name, **attrs):
    node = ET.parse(ROOT / "assets/branding" / name).getroot()
    node.attrib.update({key: str(value) for key, value in attrs.items()})
    return ET.tostring(node, encoding="unicode")


herdr = logo("herdr-ram.svg", x=64, y=87, width=108, height=100,
             viewBox="96 130 416 382", style="color:#7dcfff")
omarchy = logo("omarchy-logo.svg", x=219, y=96, width=82, height=82)

banner = f'''<svg xmlns="http://www.w3.org/2000/svg" width="1440" height="460" viewBox="0 0 1440 460" role="img" aria-labelledby="title description">
  <title id="title">Herdr × Omarchy</title>
  <desc id="description">Direct shortcuts for your terminal workspace. Super Alt M toggles the menu, Super Alt A opens an agent, and Super Alt Q cycles agents by priority.</desc>
  <defs>
    <linearGradient id="background" x1="0" y1="0" x2="1" y2="1">
      <stop stop-color="#0b2230"/>
      <stop offset="1" stop-color="#09141e"/>
    </linearGradient>
  </defs>
  <rect x="1" y="1" width="1438" height="458" rx="20" fill="url(#background)" stroke="#294452" stroke-width="2"/>
  <g font-family="Inter, DejaVu Sans, Arial, sans-serif">
    <text x="64" y="56" font-size="13" letter-spacing="3" fill="#94aab5">A COMMUNITY INTEGRATION</text>
    {herdr}
    <text x="191" y="149" font-size="30" fill="#607b8b">×</text>
    {omarchy}
    <text x="62" y="252" font-size="62" font-weight="700" letter-spacing="-2" fill="#e6edf3">Herdr × Omarchy</text>
    <text x="65" y="293" font-size="23" fill="#a8becb">Direct keys. One focused workspace.</text>
    <g font-family="DejaVu Sans Mono, monospace" font-size="14">
      <rect x="64" y="334" width="149" height="40" rx="8" fill="#112e3d" stroke="#2c5365"/>
      <text x="138.5" y="359" text-anchor="middle" fill="#7dcfff">Focused keys</text>
      <rect x="225" y="334" width="208" height="40" rx="8" fill="#112e3d" stroke="#2c5365"/>
      <text x="329" y="359" text-anchor="middle" fill="#a8becb">Strict pane focus</text>
      <rect x="445" y="334" width="178" height="40" rx="8" fill="#112e3d" stroke="#2c5365"/>
      <text x="534" y="359" text-anchor="middle" fill="#a8becb">Preview + undo</text>
    </g>
    <rect x="832" y="76" width="544" height="312" rx="14" fill="#0d202c" stroke="#294452"/>
    <circle cx="864" cy="109" r="4" fill="#9ece6a"/>
    <text x="881" y="114" font-size="12" letter-spacing="2.2" fill="#9ece6a">FOCUSED IN HERDR</text>
    <g font-family="DejaVu Sans Mono, monospace" font-size="17">
      <rect x="856" y="140" width="496" height="54" rx="7" fill="#153341"/>
      <text x="878" y="174" fill="#7dcfff">Super Alt M</text>
      <text x="1050" y="174" fill="#e6edf3">Your control menu</text>
      <rect x="856" y="206" width="496" height="54" rx="7" fill="#102936"/>
      <text x="878" y="240" fill="#7dcfff">Super Alt A</text>
      <text x="1050" y="240" fill="#e6edf3">Agent beside you</text>
      <rect x="856" y="272" width="496" height="54" rx="7" fill="#102936"/>
      <text x="878" y="306" fill="#7dcfff">Super Alt Q</text>
      <text x="1050" y="306" fill="#e6edf3">Agents by priority</text>
    </g>
    <text x="859" y="359" font-size="14" fill="#94aab5">Desktop shortcuts stay as configured.</text>
    <path d="M64 412H1376" stroke="#243d4a"/>
    <text x="64" y="439" font-size="11" letter-spacing="1.7" fill="#7592a3">HERDR PLUGIN · LINUX · OMARCHY LUA BRIDGE</text>
    <text x="1376" y="439" text-anchor="end" font-family="DejaVu Sans Mono, monospace" font-size="12" fill="#e0af68">v0.1.0 / preview</text>
  </g>
</svg>
'''

output = ROOT / "docs/assets/cover.svg"
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(banner)
print(output.relative_to(ROOT))
