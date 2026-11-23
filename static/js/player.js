const player = document.querySelector(".watch-player");
const video = player.querySelector("video");
const status = player.querySelector("[data-status]");
const quality = player.querySelector("[data-quality]");
const subtitles = player.querySelector("[data-subtitles]");
const retry = player.querySelector("[data-retry]");
const restart = player.querySelector("[data-restart]");
const csrf = player.querySelector("[name=csrfmiddlewaretoken]").value;
const storageKey = ["cinema-watch", player.dataset.device, player.dataset.profile, player.dataset.movie].join(":");
const checkpointKey = storageKey + ":position";
let tab = sessionStorage.getItem("cinema-tab") || crypto.randomUUID();
let session, token, sequence, busy = false, changing = false;
let chosenVideo, chosenSubtitle = "";
let writer = true;
let generation = 0;
let starting = true;
const channel = typeof BroadcastChannel === "function" ? new BroadcastChannel("cinema-player") : null;
const pageId = crypto.randomUUID();

if (channel) {
    channel.onmessage = ({data}) => {
        if (data.tab !== tab || data.page === pageId) return;
        if (data.kind === "probe") channel.postMessage({kind: "occupied", tab, page: pageId, target: data.page});
        if (starting && data.kind === "occupied" && data.target === pageId) {
            tab = crypto.randomUUID();
            for (const key of Object.keys(sessionStorage)) {
                if (key.startsWith("cinema-watch:")) sessionStorage.removeItem(key);
            }
        }
    };
}

async function post(url, values = {}, keepalive = false) {
    const response = await fetch(url, {method: "POST", headers: {"X-CSRFToken": csrf}, body: new URLSearchParams(values), keepalive});
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !Object.keys(data).length) {
        throw Object.assign(new Error(data.message || "Сеанс недоступен. Проверьте вход и повторите."), {code: data.code});
    }
    return data;
}

function fill(select, entries, current, emptyLabel) {
    select.replaceChildren();
    if (emptyLabel) select.add(new Option(emptyLabel, ""));
    for (const item of entries) select.add(new Option(item.label === "default" ? "Основное" : item.label, item.id));
    if (Array.from(select.options).some(option => option.value === String(current))) select.value = current;
}

function applySubtitles() {
    for (const track of video.querySelectorAll("track")) track.remove();
    const selected = session.subtitle.find(item => String(item.id) === subtitles.value);
    chosenSubtitle = subtitles.value;
    if (selected) {
        const track = document.createElement("track");
        track.kind = "subtitles";
        track.label = selected.label;
        track.src = selected.url;
        track.default = true;
        video.append(track);
        track.track.mode = "showing";
    }
}

function applyVideo(position, resume) {
    const selected = session.video.find(item => String(item.id) === quality.value);
    if (!selected) throw new Error("Видео недоступно.");
    chosenVideo = selected.id;
    changing = true;
    const revision = ++generation;
    video.addEventListener("loadedmetadata", () => {
        if (generation !== revision) return;
        const ready = () => {
            if (generation !== revision) return;
            changing = false;
            status.textContent = writer ? "Плеер готов." : "Позиция сохраняется в другом плеере.";
            if (resume) video.play().catch(() => {});
        };
        if (position > 0 && Number.isFinite(video.duration)) {
            video.addEventListener("seeked", ready, {once: true});
            video.currentTime = Math.min(position, video.duration);
        } else {
            ready();
        }
    }, {once: true});
    video.src = selected.url;
    applySubtitles();
}

function applySession(data, resume = false) {
    const previous = session;
    sequence = session?.id === data.id ? Math.max(sequence || 0, data.seq) : data.seq;
    session = data;
    writer = data.writer;
    fill(quality, data.video, chosenVideo);
    fill(subtitles, data.subtitle, chosenSubtitle, "Выключены");
    quality.parentElement.hidden = data.video.length < 2;
    subtitles.parentElement.hidden = !data.subtitle.length;
    restart.disabled = !writer;
    const checkpoint = readCheckpoint();
    const canResume = data.writer && checkpoint?.id === data.id && checkpoint.seq > data.seq;
    if (canResume) sequence = Math.max(sequence, checkpoint.seq);
    if (previous && previous.id !== data.id) sessionStorage.removeItem(checkpointKey);
    applyVideo(canResume ? checkpoint.position : data.position, resume);
}

function readCheckpoint() {
    try {
        return JSON.parse(sessionStorage.getItem(checkpointKey));
    } catch {
        return null;
    }
}

async function begin(replace = "", resume = false) {
    if (busy) return;
    busy = true;
    try {
        if (!token || replace) {
            const intent = await post(player.dataset.intent, {id: crypto.randomUUID(), tab, replace});
            token = intent.token;
            sessionStorage.setItem(storageKey, token);
        }
        applySession(await post(player.dataset.start, {token}), resume);
    } catch (error) {
        status.textContent = error.message;
        if (replace && session) {
            token = session.previousToken;
            if (token) sessionStorage.setItem(storageKey, token);
        } else if (["ended", "binding", "intent"].includes(error.code)) {
            token = null;
            session = null;
            restart.disabled = true;
            sessionStorage.removeItem(storageKey);
        }
    } finally {
        busy = false;
    }
}

async function save(withPosition, keepalive = false) {
    if (!session || busy || (withPosition && (!writer || changing || video.readyState < 1 || !Number.isFinite(video.currentTime)))) return;
    const active = session;
    const values = withPosition ? {
        seq: ++sequence, position: video.ended ? Math.ceil(video.currentTime) : Math.floor(video.currentTime), media: chosenVideo,
    } : {};
    if (withPosition) sessionStorage.setItem(checkpointKey, JSON.stringify({id: active.id, ...values}));
    try {
        const data = await post(active.activity, values, keepalive);
        if (session !== active) return;
        writer = data.writer;
        restart.disabled = !writer;
        if (!writer) status.textContent = "Позиция сохраняется в другом плеере.";
    } catch (error) {
        if (session !== active) return;
        status.textContent = error.message;
        if (error.code === "ended") {
            session = null;
            token = null;
            restart.disabled = true;
            sessionStorage.removeItem(storageKey);
            video.pause();
        }
    }
}

retry.addEventListener("click", async () => {
    if (busy) return;
    if (!session) return begin();
    await save(true);
    if (busy) return;
    if (!session) return begin();
    busy = true;
    let ended = false;
    try {
        applySession(await post(session.refresh), !video.paused);
    } catch (error) {
        status.textContent = error.message;
        ended = ["ended", "binding", "intent"].includes(error.code);
    } finally {
        busy = false;
    }
    if (ended) {
        session = null;
        token = null;
        restart.disabled = true;
        sessionStorage.removeItem(storageKey);
        await begin();
    }
});
restart.addEventListener("click", () => {
    if (busy || !session) return;
    session.previousToken = token;
    begin(session.id, !video.paused);
});
quality.addEventListener("change", () => { if (session && !busy) applyVideo(video.currentTime, !video.paused); });
subtitles.addEventListener("change", () => { if (session) applySubtitles(); });
video.addEventListener("pause", () => save(true));
video.addEventListener("seeked", () => save(true));
video.addEventListener("ended", () => save(true));
window.addEventListener("pagehide", () => save(true, true));
video.addEventListener("error", () => { status.textContent = "Не удалось загрузить видео. Нажмите «Повторить»."; });
setInterval(() => { if (!video.paused) save(true); }, 14000);
setInterval(() => save(false), 25000);

if (channel) channel.postMessage({kind: "probe", tab, page: pageId});
setTimeout(() => {
    starting = false;
    sessionStorage.setItem("cinema-tab", tab);
    token = sessionStorage.getItem(storageKey);
    begin();
}, channel ? 180 : 0);
