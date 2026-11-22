"use strict";

window.browserBridge = (() => {
  const pyodideRoot = "https://cdn.jsdelivr.net/pyodide/v0.27.2/full/";
  const modules = ["converter.py", "highscore_bridge.py", "watcher.py", "sample_data_gen.py", "bridge_web.py"];
  const scanExtensions = /\.(hdf5|h5|xrdml|xy|xye|asc|dat)$/i;
  let engine;
  let importNumber = 0;
  let inputLabel = "";

  async function start() {
    if (!window.loadPyodide) {
      await new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = pyodideRoot + "pyodide.js";
        script.onload = resolve;
        script.onerror = () => reject(new Error("Could not load the browser conversion engine. Check your connection and reload."));
        document.head.append(script);
      });
    }
    const py = await loadPyodide({ indexURL: pyodideRoot });
    await Promise.all([
      py.loadPackage(["numpy", "h5py"]),
      ...modules.map(async name => {
        const response = await fetch(name);
        if (!response.ok) throw new Error(`Could not load ${name} (HTTP ${response.status}).`);
        py.FS.writeFile(name, await response.text());
      }),
    ]);
    return { py, dispatch: py.runPython("from bridge_web import request\nrequest") };
  }

  function selectFiles(directory) {
    return new Promise(resolve => {
      const input = document.createElement("input");
      input.type = "file";
      input.multiple = true;
      input.hidden = true;
      if (directory) input.webkitdirectory = true;
      else input.accept = ".hdf5,.h5,.xrdml,.xy,.xye,.asc,.dat";
      const finish = files => {
        input.remove();
        resolve(files);
      };
      input.onchange = () => finish(Array.from(input.files));
      input.oncancel = () => finish([]);
      document.body.append(input);
      input.click();
    });
  }

  async function importFiles(py, files) {
    const root = `/work/imports/${++importNumber}`;
    py.FS.mkdirTree(root);
    for (const file of files) {
      const parts = file.webkitRelativePath ? file.webkitRelativePath.split("/").slice(1) : [file.name];
      if (parts.some(part => !part || part === "." || part === ".." || part.includes("\\"))) {
        throw new Error("The selected file has an unsupported path.");
      }
      const relative = parts.join("/");
      if (!scanExtensions.test(relative) && !/^highscore_results\/.+\.csv$/i.test(relative)) continue;
      py.FS.mkdirTree(`${root}/${parts.slice(0, -1).join("/")}`);
      py.FS.writeFile(`${root}/${relative}`, new Uint8Array(await file.arrayBuffer()));
    }
    return root;
  }

  function downloadZip(py) {
    const url = URL.createObjectURL(new Blob([py.FS.readFile("/work/converted.zip")], { type: "application/zip" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = "XRD-converted.zip";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  }

  async function setupGuide() {
    const [guide_md, pascal_script] = await Promise.all(
      ["HIGHSCORE_SETUP_GUIDE.md", "auto_search_match_icdd.pas"].map(async name => {
        const response = await fetch(name);
        if (!response.ok) throw new Error(`Could not load ${name}.`);
        return response.text();
      }),
    );
    return { guide_md, pascal_script };
  }

  async function request(url, options) {
    const { pathname, searchParams } = new URL(url, location.origin);
    if (pathname === "/api/setup-guide") return setupGuide();
    const payload = options.body ? JSON.parse(options.body) : Object.fromEntries(searchParams);
    let files;
    if (pathname === "/api/browse-folder") {
      files = await selectFiles(payload.target !== "input_files");
      if (files.length && !files.some(file => scanExtensions.test(file.name))) {
        throw new Error("The selection contains no supported XRD files.");
      }
    }
    engine ??= start();
    const { py, dispatch } = await engine;
    let route = pathname;
    if (files?.length) payload.path = await importFiles(py, files);
    else if (files) route = "/api/state";
    let response = JSON.parse(dispatch(route, JSON.stringify(payload)));
    if (response.error) throw new Error(response.error);
    if (files?.length) {
      inputLabel = files[0].webkitRelativePath.split("/")[0] || `${files.length} selected file(s)`;
      response.path = inputLabel;
    } else if (files) {
      response = { ok: false, state: response };
    } else if (route === "/api/generate-demo") {
      inputLabel ||= "Demo data";
    }
    const state = response.state || (route === "/api/state" ? response : null);
    if (state) {
      state.config.input_dir = inputLabel;
      state.config.custom_output_dir = "Browser downloads (.zip)";
    }
    if (response.download) downloadZip(py);
    return response;
  }

  function configure() {
    const source = document.getElementById("inputDir");
    source.readOnly = true;
    const files = document.createElement("button");
    files.type = "button";
    files.textContent = "Choose files";
    files.onclick = () => browseFolder("input_files");
    source.parentElement.append(files);
    document.getElementById("customOutputDir").readOnly = true;
    const desktopOnly = "#watchBtn, #customOutWrap button, #outputMode, #highscoreExe, #autoLaunch, [onclick='browseExe()']";
    document.querySelectorAll(desktopOnly).forEach(control => {
      control.disabled = true;
      control.title = "Available in the desktop app";
    });
    document.querySelector(".run-note").textContent = "Files stay on this device. Converted files download as a ZIP. Live watch is available in the desktop app.";
  }

  return { configure, request };
})();
