/**
 * FakeDeafen+ for Vencord
 * Built for VencordGracefulPatch
 *
 * Appears deafened & muted to everyone else in the voice channel,
 * while allowing you to secretly hear incoming audio and speak live.
 *
 * Placed cleanly inside Discord's floating call toolbar at the bottom center,
 * right next to the Mute/Microphone button.
 *
 * Controls:
 * - Click the FakeDeafen+ button in the center call toolbar
 * - Or press F8 / Ctrl + Shift + Q anytime Discord is focused
 */

(() => {
    // Clean up any old elements from previous injections
    document.getElementById("vc-fake-deafen-call-btn")?.remove();
    document.getElementById("vc-fake-deafen-slot")?.remove();
    document.getElementById("vc-fake-deafen-style")?.remove();
    document.getElementById("vc-fake-deafen-tooltip")?.remove();

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
                min-width: 32px;
                min-height: 32px;
                border-radius: 8px;
                border: 0;
                background-color: transparent;
                color: var(--interactive-normal, #b5bac1);
                margin: 0 2px;
            }
            #${BUTTON_ID} svg, #${BUTTON_ID} path {
                pointer-events: none;
            }
            #${BUTTON_ID}:hover {
                background-color: var(--background-modifier-hover, rgba(78, 80, 88, 0.32));
                color: var(--interactive-hover, #dbdee1);
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

    // Finds the floating call toolbar in the bottom-middle of the screen
    function findCenterToolbarPlacement() {
        const allButtons = Array.from(document.querySelectorAll('button[aria-label]'));

        // Target the floating toolbar in the bottom-middle (horizontal > 320px, vertical > 60% of viewport)
        const centerButtons = allButtons.filter(b => {
            const r = b.getBoundingClientRect();
            return r.width > 0 && r.height > 0 && r.left > 320 && r.bottom > window.innerHeight * 0.60;
        });

        if (centerButtons.length > 0) {
            // Find the Mute/Microphone button in the center toolbar
            const micBtn = centerButtons.find(b => {
                const label = (b.getAttribute('aria-label') || '').toLowerCase();
                return label.includes('mute') || label.includes('mic') || label.includes('silenciar');
            });

            // Find the Camera button in the center toolbar
            const cameraBtn = centerButtons.find(b => {
                const label = (b.getAttribute('aria-label') || '').toLowerCase();
                return label.includes('camera') || label.includes('cámara') || label.includes('video');
            });

            if (micBtn && cameraBtn) {
                // Find the pill container holding both Mic and Camera
                let pill = micBtn.parentElement;
                while (pill && pill !== document.body && !pill.contains(cameraBtn)) {
                    pill = pill.parentElement;
                }
                if (pill) {
                    let micWrapper = micBtn;
                    while (micWrapper.parentElement && micWrapper.parentElement !== pill) {
                        micWrapper = micWrapper.parentElement;
                    }
                    return { container: pill, anchor: micWrapper.nextElementSibling, refBtn: micBtn };
                }
            }

            if (micBtn) {
                return { container: micBtn.parentElement, anchor: micBtn.nextElementSibling, refBtn: micBtn };
            }

            const first = centerButtons[0];
            return { container: first.parentElement, anchor: first.nextElementSibling, refBtn: first };
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

        // Ensure old slots from sidebar / account bar are cleaned up
        document.getElementById("vc-fake-deafen-slot")?.remove();

        const point = findCenterToolbarPlacement();
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

            // Inherit native classes from sibling button for seamless sizing and ripple
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

    console.log("%c[FakeDeafen+] Ready! Button positioned in middle bottom call toolbar next to Mic (Hotkey: F8 or Ctrl+Shift+Q).", "color: #57F287; font-weight: bold;");
})();
