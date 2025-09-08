// main.js - handles interactions, SSE & UI
let currentTask = null;
const startBtn = document.getElementById('startBtn');
const subs = document.getElementById('subs');
const subsFile = document.getElementById('subsFile');
const startResult = document.getElementById('startResult');
const liveArea = document.getElementById('liveArea');
const progressFill = document.getElementById('progressFill');
const resultsDiv = document.getElementById('results');
const refreshBtn = document.getElementById('refreshBtn');
const downloadLink = document.getElementById('downloadLink');
const workersInput = document.getElementById('workers');

startBtn.onclick = async () => {
  let list = subs.value.trim();
  if (!list && subsFile.files.length == 0) {
    alert("Colle une liste ou upload un fichier .txt");
    return;
  }
  if (subsFile.files.length > 0) {
    const f = subsFile.files[0];
    const txt = await f.text();
    list = txt.trim();
  }
  const body = { subdomains: list, max_workers: parseInt(workersInput.value || "8") };
  startResult.innerText = "Création du job...";
  const res = await fetch("/start_scan", {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify(body)
  });
  const j = await res.json();
  if (j.error) {
    startResult.innerText = "Erreur: " + j.error;
    return;
  }
  const task_id = j.task_id;
  currentTask = task_id;
  startResult.innerText = `Job lancé: ${task_id}`;
  resultsDiv.innerText = "En attente de résultats...";
  startSSE(task_id);
};

function startSSE(task_id){
  liveArea.innerText = "Connexion au flux...";
  const es = new EventSource(`/stream/${task_id}`);
  es.onmessage = (e) => {
    try {
      const p = JSON.parse(e.data);
      const log = p.log || "";
      const pr = p.progress || 0;
      const status = p.status || "";
      liveArea.innerText = (new Date()).toLocaleTimeString() + " | " + log + "\n\n" + liveArea.innerText;
      progressFill.style.width = pr + "%";
      if (status === "finished") {
        liveArea.innerText = "Task finished, fetching results...";
        fetchResults(task_id);
        es.close();
      }
    } catch (err) {
      console.warn("SSE parse error", err);
    }
  };
  es.onerror = (err) => {
    console.warn("SSE error", err);
    // try reconnect later automatically by browser
  };
}

async function fetchResults(task_id){
  const s = await fetch(`/task_status/${task_id}`);
  const j = await s.json();
  if (j.error) {
    resultsDiv.innerText = "Error: " + j.error;
    return;
  }
  if (j.result_path) {
    // download link
    downloadLink.style.display = "inline-block";
    downloadLink.href = `/download/${task_id}`;
    downloadLink.innerText = "Télécharger JSON";
    // fetch the results file
    const res = await fetch(`/download/${task_id}`);
    const blob = await res.blob();
    const text = await blob.text();
    try {
      const parsed = JSON.parse(text);
      resultsDiv.innerText = JSON.stringify(parsed, null, 2);
    } catch (e) {
      resultsDiv.innerText = text;
    }
  } else {
    resultsDiv.innerText = "Result not ready yet.";
  }
}

refreshBtn.onclick = () => {
  if (!currentTask) { alert("Aucun job connu."); return; }
  fetchResults(currentTask);
};
