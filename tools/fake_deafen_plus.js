/**
 * FakeDeafen+ for Vencord
 * Built for VencordGracefulPatch
 *
 * Appears deafened & muted to everyone else in the voice channel,
 * while allowing you to secretly hear incoming audio and speak live.
 *
 * Positioned cleanly inside Discord's main call control toolbar (middle pill next to Soundboard)
 * Controls:
 * - Click the FakeDeafen+ button in the call control toolbar
 * - Or press F8 / Ctrl + Shift + Q anytime Discord is focused
 */

(() => {
    if (window.__fakeDeafenPlusLoaded) {
        // Clean up previous elements if reloading
        document.getElementById("vc-fake-deafen-call-btn")?.remove();
        document.getElementById("vc-fake-deafen-slot")?.remove();
        document.getElementById("vc-fake-deafen-style")?.remove();
        document.getElementById("vc-fake-deafen-tooltip")?.remove();
    }
    window.__fakeDeafenPlusLoaded = true;

    const BUTTON_ID = "vc-fake-deafen-call-btn";
    const STYLE_ID = "vc-fake-deafen-style";
    const TOOLTIP_ID = "vc-fake-deafen-tooltip";

    let active = false;
    let activeChannelId = null;
    let observer = null;
    let pendingButtonFrame = 0;
    let audioContext = null;
    const patchedSockets = new Map();

    function getAudioContext() {
        try {
            audioContext = audioContext || new (window.AudioContext || window.webkitAudioContext)();
            return audioContext;
        } catch (e) {
            return null;
        }
    }

    function playToggleSound(enabled) {
        const ctx = getAudioContext();
        if (!ctx) return;
        ctx.resume().then(() => {
            const now = ctx.currentTime;
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();

            osc.type = "sine";
            osc.frequency.setValueAtTime(enabled ? 520 : 690, now);
            osc.frequency.exponentialRampToValueAtTime(enabled ? 760 : 430, now + 0.13);

            gain.gain.setValueAtTime(0.0001, now);
            gain.gain.exponentialRampToValueAtTime(0.055, now + 0.012);
            gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.18);

            osc.connect(gain);
            gain.connect(ctx.destination);

            osc.start(now);
            osc.stop(now + 0.19);
        }).catch(() => {});
    }

    function getStores() {
        const Common = window.Vencord?.Webpack?.Common || {};
        const findByProps = window.Vencord?.Webpack?.findByProps || (() => null);

        const SelectedChannelStore = Common.SelectedChannelStore || findByProps("getVoiceChannelId");
        const ChannelStore = Common.ChannelStore || findByProps("getChannel", "getDMFromUserId");
        const MediaEngineStore = Common.MediaEngineStore || findByProps("isDeaf", "isMute");
        const wsModule = findByProps("getSocket");

        return { SelectedChannelStore, ChannelStore, MediaEngineStore, wsModule, Common };
    }

    function getVoiceChannelId() {
        const { SelectedChannelStore } = getStores();
        return SelectedChannelStore?.getVoiceChannelId?.()
            ?? SelectedChannelStore?.getVoiceChannel?.()?.id
            ?? null;
    }

    function getActualMute() {
        const { MediaEngineStore } = getStores();
        return Boolean(MediaEngineStore?.isMute?.() ?? MediaEngineStore?.isSelfMute?.() ?? false);
    }

    function getActualDeaf() {
        const { MediaEngineStore } = getStores();
        return Boolean(MediaEngineStore?.isDeaf?.() ?? MediaEngineStore?.isSelfDeaf?.() ?? false);
    }

    function getSocket() {
        const { wsModule } = getStores();
        try {
            const socket = wsModule?.getSocket?.();
            return socket && typeof socket.send === "function" ? socket : null;
        } catch (e) {
            return null;
        }
    }

    function deactivateForChannelChange() {
        if (!active) return;
        active = false;
        activeChannelId = null;
        scheduleButtonUpdate();
        showNotification("FakeDeafen+ deactivated (channel changed).");
    }

    function patchCurrentSocket() {
        const socket = getSocket();
        if (!socket) return null;
        if (patchedSockets.has(socket)) return socket;

        const original = socket.send;
        const wrapped = function (op, data, ...args) {
            let outgoing = data;
            if (op === 4 && active && outgoing && typeof outgoing === "object") {
                const nextChannelId = typeof outgoing.channel_id === "string" ? outgoing.channel_id : null;
                if (!nextChannelId || (activeChannelId && nextChannelId !== activeChannelId)) {
                    deactivateForChannelChange();
                } else {
                    outgoing = { ...outgoing };
                    outgoing.self_mute = true;
                    outgoing.self_deaf = true;
                }
            }
            return original.apply(this, [op, outgoing, ...args]);
        };

        try {
            socket.send = wrapped;
            patchedSockets.set(socket, { original, wrapped });
            return socket;
        } catch (e) {
            console.error("[FakeDeafen+] Socket patch failed:", e);
            return null;
        }
    }

    function refreshVoiceState(useFakeState) {
        const channelId = getVoiceChannelId();
        if (!channelId) return false;
        const socket = patchCurrentSocket();
        if (!socket) return false;

        const { ChannelStore } = getStores();
        const channel = ChannelStore?.getChannel?.(channelId);

        const payload = {
            guild_id: channel?.guild_id ?? null,
            channel_id: channelId,
            self_mute: useFakeState ? true : getActualMute(),
            self_deaf: useFakeState ? true : getActualDeaf(),
            self_video: false,
            flags: 0
        };

        try {
            socket.send(4, payload);
            return true;
        } catch (e) {
            console.error("[FakeDeafen+] Failed to send voice state update:", e);
            return false;
        }
    }

    function showNotification(msg, type = "info") {
        const { Common } = getStores();
        if (Common?.showToast && Common?.Toasts) {
            try {
                Common.showToast(msg, type === "error" ? Common.Toasts.Type.FAILURE : Common.Toasts.Type.SUCCESS);
                return;
            } catch (e) {}
        }
        console.log(`[FakeDeafen+] ${msg}`);
    }

    function toggleActive() {
        const channelId = getVoiceChannelId();
        if (!active) {
            if (!channelId) {
                showNotification("Join a voice channel or call first!", "error");
                playToggleSound(false);
                return false;
            }
            if (!patchCurrentSocket()) {
                showNotification("Could not connect to Discord voice socket.", "error");
                return false;
            }
            active = true;
            activeChannelId = channelId;
            if (!refreshVoiceState(true)) {
                active = false;
                activeChannelId = null;
                scheduleButtonUpdate();
                return false;
            }
            playToggleSound(true);
            showNotification("FakeDeafen+ ON — Deafened to others (Microphone & Audio Active)");
        } else {
            active = false;
            activeChannelId = null;
            refreshVoiceState(false);
            playToggleSound(false);
            showNotification("FakeDeafen+ OFF — Restored normal voice state");
        }
        scheduleButtonUpdate();
        return active;
    }

    // --- UI Button & Styling ---
    function installButtonStyles() {
        if (document.getElementById(STYLE_ID)) return;
        const style = document.createElement("style");
        style.id = STYLE_ID;
        style.textContent = `
            #${BUTTON_ID} {
                box-sizing: border-box;
                display: inline-flex;
                align-items: center;
                justify-content: center;
                cursor: pointer;
                transition: background-color 150ms ease, color 150ms ease;
            }
            #${BUTTON_ID} svg, #${BUTTON_ID} path {
                pointer-events: none;
            }
            #${BUTTON_ID}[data-active="true"] {
                background-color: var(--status-danger, #f23f42) !important;
                color: #ffffff !important;
            }
            #${BUTTON_ID}[data-active="true"]:hover {
                background-color: color-mix(in srgb, var(--status-danger, #f23f42) 82%, black) !important;
            }
            #${TOOLTIP_ID} {
                position: fixed;
                z-index: 10000;
                max-width: 260px;
                padding: 8px 10px;
                border: 1px solid var(--border-subtle, rgba(255, 255, 255, 0.08));
                border-radius: 5px;
                background: var(--bg-surface-overlay, #232428);
                color: var(--text-default, #f2f3f5);
                box-shadow: 0 8px 16px rgba(0, 0, 0, 0.24);
                font-size: 14px;
                font-weight: 500;
                line-height: 18px;
                white-space: nowrap;
                pointer-events: none;
                opacity: 0;
                transform: translate(-50%, 4px);
                transition: opacity 80ms ease, transform 80ms ease;
            }
            #${TOOLTIP_ID}[data-visible="true"] {
                opacity: 1;
                transform: translate(-50%, 0);
            }
        `;
        document.head.appendChild(style);
    }

    function buttonIcon() {
        return `
            <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true">
                <path fill="currentColor" d="M12 3a5 5 0 0 0-5 5v3.17A3 3 0 0 0 5 14v1a3 3 0 0 0 3 3h1v-6H8V8a4 4 0 0 1 8 0v4h-1v6h1a3 3 0 0 0 3-3v-1a3 3 0 0 0-2-2.83V8a5 5 0 0 0-5-5Z"/>
                <path fill="currentColor" d="m4.7 3.3 16 16-1.4 1.4-16-16 1.4-1.4Z"/>
            </svg>`;
    }

    function showTooltip(button) {
        hideTooltip();
        const tooltip = document.createElement("div");
        tooltip.id = TOOLTIP_ID;
        tooltip.textContent = active ? "FakeDeafen+ (Active — Hearing & Speaking)" : "FakeDeafen+ (Appear Deafened)";
        document.body.appendChild(tooltip);

        const bRect = button.getBoundingClientRect();
        const tRect = tooltip.getBoundingClientRect();
        const desiredLeft = bRect.left + bRect.width / 2;
        const halfWidth = tRect.width / 2;
        const safeLeft = Math.min(window.innerWidth - halfWidth - 8, Math.max(halfWidth + 8, desiredLeft));

        tooltip.style.left = `${safeLeft}px`;
        tooltip.style.top = `${Math.max(8, bRect.top - tRect.height - 10)}px`;
        requestAnimationFrame(() => {
            if (tooltip.isConnected) tooltip.dataset.visible = "true";
        });
    }

    function hideTooltip() {
        document.getElementById(TOOLTIP_ID)?.remove();
    }

    function findCallBarInsertionPoint() {
        // 1. Look for Soundboard button in the call toolbar
        const soundboard = document.querySelector('button[aria-label*="Soundboard" i], button[aria-label*="Sonidos" i]');
        if (soundboard && soundboard.parentElement) {
            return { container: soundboard.parentElement, anchor: soundboard.nextElementSibling, refBtn: soundboard };
        }

        // 2. Look for Start an Activity / Activities button in the call toolbar
        const activities = document.querySelector('button[aria-label*="Activit" i], button[aria-label*="Activida" i]');
        if (activities && activities.parentElement) {
            return { container: activities.parentElement, anchor: activities.nextElementSibling, refBtn: activities };
        }

        // 3. Look for Share Your Screen button in the call toolbar
        const screenShare = document.querySelector('button[aria-label*="Share" i][aria-label*="Screen" i], button[aria-label*="Pantalla" i]');
        if (screenShare && screenShare.parentElement) {
            return { container: screenShare.parentElement, anchor: screenShare.nextElementSibling, refBtn: screenShare };
        }

        // 4. Look for Disconnect button's previous sibling toolbar section
        const disconnect = document.querySelector('button[aria-label*="Disconnect" i], button[aria-label*="Desconectar" i]');
        if (disconnect) {
            const callBar = disconnect.closest('[class*="wrapper_"], [class*="callContainer_"]') || disconnect.parentElement?.parentElement;
            if (callBar) {
                const buttons = Array.from(callBar.querySelectorAll('button[aria-label]'));
                for (const b of buttons) {
                    const label = (b.getAttribute('aria-label') || '').toLowerCase();
                    if (b !== disconnect && !label.includes('mute') && !label.includes('camera')) {
                        return { container: b.parentElement, anchor: b.nextElementSibling, refBtn: b };
                    }
                }
            }
        }

        return null;
    }

    function scheduleButtonUpdate() {
        if (pendingButtonFrame) return;
        pendingButtonFrame = requestAnimationFrame(() => {
            pendingButtonFrame = 0;
            updateButton();
        });
    }

    function updateButton() {
        installButtonStyles();

        // Clean up any old bottom-panel slot if present
        document.getElementById("vc-fake-deafen-slot")?.remove();

        const point = findCallBarInsertionPoint();
        let btn = document.getElementById(BUTTON_ID);

        if (!point) {
            btn?.remove();
            return;
        }

        const { container, anchor, refBtn } = point;

        if (!btn || btn.parentElement !== container) {
            btn?.remove();
            btn = document.createElement("button");
            btn.id = BUTTON_ID;
            btn.type = "button";
            btn.innerHTML = buttonIcon();

            // Inherit native button classes from sibling for identical sizing & ripple
            if (refBtn && refBtn.className) {
                btn.className = refBtn.className;
            }

            btn.addEventListener("mouseenter", () => showTooltip(btn));
            btn.addEventListener("mouseleave", hideTooltip);
            btn.addEventListener("focus", () => showTooltip(btn));
            btn.addEventListener("blur", hideTooltip);
            btn.addEventListener("click", (e) => {
                e.preventDefault();
                e.stopPropagation();
                hideTooltip();
                toggleActive();
            });

            container.insertBefore(btn, anchor);
        }

        btn.setAttribute("aria-label", "FakeDeafen+");
        btn.setAttribute("aria-pressed", String(active));
        btn.dataset.active = String(active);
    }

    function installObserver() {
        if (observer || !document.body) return;
        observer = new MutationObserver(scheduleButtonUpdate);
        observer.observe(document.body, { childList: true, subtree: true });
        scheduleButtonUpdate();
    }

    // Hotkey listener: F8 or Ctrl+Shift+Q
    window.addEventListener("keydown", (e) => {
        if (e.key === "F8" && !e.ctrlKey && !e.altKey && !e.shiftKey) {
            e.preventDefault();
            toggleActive();
        } else if (e.ctrlKey && e.shiftKey && (e.code === "KeyQ" || e.key.toLowerCase() === "q")) {
            e.preventDefault();
            toggleActive();
        }
    });

    window.toggleFakeDeafen = toggleActive;

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", () => {
            installObserver();
            patchCurrentSocket();
        });
    } else {
        installObserver();
        patchCurrentSocket();
    }

    console.log("%c[FakeDeafen+] Ready! Button placed in Call Control Toolbar (Hotkey: F8 or Ctrl+Shift+Q).", "color: #57F287; font-weight: bold;");
})();
