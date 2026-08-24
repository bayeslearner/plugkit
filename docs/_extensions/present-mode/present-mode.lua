-- present-mode.lua — Quarto filter that injects the present-mode CSS + JS into
-- HTML output as a bundled, namespaced dependency (site_libs/present-mode/…).
-- This is the idiomatic Quarto add-on mechanism: no loose files in the project,
-- no include-after-body editing — just `filters: [present-mode]` under format.html.
function Pandoc(doc)
  if quarto.doc.isFormat("html:js") then
    quarto.doc.addHtmlDependency({
      name = "present-mode",
      version = "1.0.0",
      scripts = { "present-mode.js" },
      stylesheets = { "present-mode.css" }
    })
  end
  return doc
end
