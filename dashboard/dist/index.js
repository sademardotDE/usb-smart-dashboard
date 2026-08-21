(function () {
  "use strict";

  // USB Smart Dashboard — dashboard panel.
  // Plain IIFE, no build step. Uses window.__HERMES_PLUGIN_SDK__ for React
  // and shadcn primitives, and talks to the plugin's backend API at
  // /api/plugins/usb-smart-dashboard/ (config GET/PUT + live status GET).

  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  var React = SDK.React;
  var h = React.createElement;
  var hooks = SDK.hooks || {};
  var components = SDK.components || {};

  var useState = hooks.useState || React.useState;
  var useEffect = hooks.useEffect || React.useEffect;

  var useToast = hooks.useToast || function () {
    return { showToast: function () {}, toast: null };
  };

  var Checkbox = components.Checkbox || function (props) {
    return h("input", {
      type: "checkbox",
      checked: !!props.checked,
      className: props.className,
      id: props.id,
      onChange: function (e) {
        if (props.onCheckedChange) props.onCheckedChange(e.target.checked);
      },
    });
  };

  var API_BASE = "/api/plugins/usb-smart-dashboard";

  var DEFAULTS = {
    enabled: true,
    driver: "turing",
    port: "",
    baudrate: 115200,
    brightness: 25,
    display_width: 480,
    display_height: 320,
    font_size: 28,
  };

  var DRIVER_OPTIONS = ["turing", "serial", "file", "auto"];

  var FIELDS = [
    { key: "enabled", label: "Enabled", type: "checkbox" },
    { key: "driver", label: "Driver", type: "select", options: DRIVER_OPTIONS },
    {
      key: "port", label: "Port", type: "text",
      help: "Serial port (e.g. /dev/ttyACM0). Empty = autodetect.",
    },
    { key: "baudrate", label: "Baudrate", type: "number" },
    { key: "brightness", label: "Brightness", type: "range", min: 0, max: 100 },
    { key: "display_width", label: "Panel width (px)", type: "number" },
    { key: "display_height", label: "Panel height (px)", type: "number" },
    { key: "font_size", label: "Font size (px)", type: "number" },
  ];

  function selectChangeHandler(onChange) {
    return {
      onValueChange: function (v) { onChange(v == null ? "" : v); },
      onChange: function (e) {
        var v = e && e.target ? e.target.value : e;
        onChange(v == null ? "" : v);
      },
    };
  }

  function ConfigField(props) {
    var field = props.field;
    var value = props.value;
    var id = "usb-cfg-" + field.key;

    var control;
    if (field.type === "checkbox") {
      control = h(Checkbox, {
        id: id,
        checked: !!value,
        onCheckedChange: function (v) { props.onChange(field.key, !!v); },
      });
    } else if (field.type === "select") {
      control = h(components.Select, Object.assign({
        id: id,
        value: String(value == null ? "" : value),
      }, selectChangeHandler(function (v) { props.onChange(field.key, v); })),
        field.options.map(function (opt) {
          return h(components.SelectOption, { key: opt, value: opt }, opt);
        }),
      );
    } else if (field.type === "range") {
      control = h("div", {
        className: "usb-range-row",
        style: { display: "flex", alignItems: "center", gap: "0.5rem" },
      },
        h("input", {
          id: id,
          type: "range",
          min: field.min,
          max: field.max,
          value: value == null ? field.min : value,
          onChange: function (e) { props.onChange(field.key, Number(e.target.value)); },
          style: { flex: "1" },
        }),
        h("span", { className: "usb-range-val" }, String(value == null ? field.min : value)),
      );
    } else if (field.type === "number") {
      control = h(components.Input, {
        id: id,
        type: "number",
        value: value == null ? "" : value,
        onChange: function (e) { props.onChange(field.key, Number(e.target.value)); },
      });
    } else {
      control = h(components.Input, {
        id: id,
        type: "text",
        value: value == null ? "" : value,
        onChange: function (e) { props.onChange(field.key, e.target.value); },
      });
    }

    return h("div", { className: "usb-field" },
      h(components.Label, { htmlFor: id }, field.label),
      control,
      field.help ? h("p", {
        className: "usb-field-help",
        style: { fontSize: "0.8rem", color: "var(--muted-foreground, #888)", margin: "0.25rem 0 0" },
      }, field.help) : null,
    );
  }

  // Small gauge-ish stat row: label + value (+ a simple bar for percentage).
  function StatRow(props) {
    var label = props.label;
    var value = props.value;
    var pct = props.pct;
    var barColor = props.color || "var(--accent, #4ade80)";
    if (value == null) value = "—";
    var bar = null;
    if (pct != null) {
      var clamped = Math.max(0, Math.min(100, pct));
      bar = h("div", { style: { background: "var(--muted, #1f2937)", borderRadius: "4px", height: "6px", width: "100%", marginTop: "4px" } },
        h("div", { style: { background: barColor, borderRadius: "4px", height: "6px", width: clamped + "%" } })
      );
    }
    return h("div", { style: { marginBottom: "0.75rem" } },
      h("div", { style: { display: "flex", justifyContent: "space-between", fontSize: "0.85rem" } },
        h("span", { style: { color: "var(--muted-foreground, #888)" } }, label),
        h("span", { style: { fontVariantNumeric: "tabular-nums" } }, value)
      ),
      bar
    );
  }

  function StatusPanel() {
    var statusState = useState(null);
    var status = statusState[0];
    var setStatus = statusState[1];
    var errState = useState(null);
    var err = errState[0];
    var setErr = errState[1];

    useEffect(function () {
      var alive = true;
      function poll() {
        SDK.fetchJSON(API_BASE + "/status")
          .then(function (data) {
            if (!alive) return;
            setStatus(data);
            setErr(null);
          })
          .catch(function (e) {
            if (!alive) return;
            setErr(e && e.message ? e.message : String(e));
          });
      }
      poll();
      var iv = setInterval(poll, 2000);
      return function () { alive = false; clearInterval(iv); };
    }, []);

    return h(components.Card, null,
      h(components.CardHeader, null,
        h(components.CardTitle, null, "System Status"),
        status && status.host
          ? h("p", { style: { fontSize: "0.8rem", color: "var(--muted-foreground, #888)" } },
              status.host + (status.time ? "  ·  " + status.time : ""))
          : null
      ),
      h(components.CardContent, null,
        err ? h("p", { style: { color: "var(--destructive, #ef4444)" } }, "Status unavailable: " + err)
        : !status ? h("p", { style: { color: "var(--muted-foreground, #888)" } }, "Loading…")
        : h("div", null,
            h(StatRow, { label: "CPU", value: status.cpu, pct: parseFloat(String(status.cpu).replace("%", "")) || null, color: "var(--accent, #4ade80)" }),
            h(StatRow, { label: "Memory", value: status.mem, pct: status.mem_pct, color: "var(--accent, #4ade80)" }),
            h(StatRow, { label: "Load", value: status.load }),
            h(StatRow, { label: "Thermal", value: status.temp }),
            h(StatRow, { label: "Uptime", value: status.uptime })
          )
      )
    );
  }

  function SettingsPanel() {
    var toastApi = useToast();
    var showToast = toastApi.showToast;

    var configState = useState(null);
    var config = configState[0];
    var setConfig = configState[1];

    var draftState = useState(null);
    var draft = draftState[0];
    var setDraft = draftState[1];

    var savingState = useState(false);
    var saving = savingState[0];
    var setSaving = savingState[1];

    var errorState = useState(null);
    var error = errorState[0];
    var setError = errorState[1];

    useEffect(function () {
      var alive = true;
      SDK.fetchJSON(API_BASE + "/config")
        .then(function (data) {
          if (!alive) return;
          var merged = Object.assign({}, DEFAULTS, data || {});
          setConfig(merged);
          setDraft(merged);
        })
        .catch(function (err) {
          if (!alive) return;
          setError(err && err.message ? err.message : String(err));
        });
      return function () { alive = false; };
    }, []);

    if (error) {
      return h(components.Card, null,
        h(components.CardHeader, null, h(components.CardTitle, null, "Settings")),
        h(components.CardContent, null,
          h("p", { style: { color: "var(--destructive, #ef4444)" } }, "Failed to load config: " + error))
      );
    }

    if (!config || !draft) {
      return h(components.Card, null,
        h(components.CardHeader, null, h(components.CardTitle, null, "Settings")),
        h(components.CardContent, null, h("p", null, "Loading…"))
      );
    }

    function setField(key, value) {
      setDraft(function (prev) {
        var next = Object.assign({}, prev);
        next[key] = value;
        return next;
      });
    }

    function save() {
      setSaving(true);
      SDK.fetchJSON(API_BASE + "/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(draft),
      }).then(function (saved) {
        var merged = Object.assign({}, DEFAULTS, saved || {});
        setConfig(merged);
        setDraft(merged);
        showToast("Settings saved", "success");
      }).catch(function (err) {
        showToast("Save failed: " + (err && err.message ? err.message : String(err)), "error");
      }).then(function () {
        setSaving(false);
      });
    }

    var changed = JSON.stringify(config) !== JSON.stringify(draft);

    return h(components.Card, null,
      h(components.CardHeader, null,
        h(components.CardTitle, null, "Settings"),
        h("p", { style: { fontSize: "0.85rem", color: "var(--muted-foreground, #888)" } },
          "Turing Smart Screen 3.5\" status display — driver settings.")
      ),
      h(components.CardContent, null,
        h("div", { className: "usb-fields" },
          FIELDS.map(function (f) {
            return h(ConfigField, {
              key: f.key,
              field: f,
              value: draft[f.key],
              onChange: setField,
            });
          }),
        ),
        h("div", {
          className: "usb-actions",
          style: { marginTop: "1rem", display: "flex", gap: "0.5rem", alignItems: "center" },
        },
          h(components.Button, { onClick: save, disabled: saving || !changed },
            saving ? "Saving…" : "Save"),
          changed ? h("span", {
            className: "usb-dirty-hint",
            style: { fontSize: "0.8rem", color: "var(--muted-foreground, #888)" },
          }, "Unsaved changes") : null,
        ),
      ),
    );
  }

  function UsbSmartDashboardPage() {
    return h("div", { className: "usb-page", style: { display: "flex", flexDirection: "column", gap: "1rem" } },
      h(StatusPanel),
      h(SettingsPanel)
    );
  }

  window.__HERMES_PLUGINS__.register("usb-smart-dashboard", UsbSmartDashboardPage);
})();
