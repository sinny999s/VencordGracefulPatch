/**
 * Vencord Fake Deafen Script
 * Shows you as muted and deafened to everyone in the voice channel,
 * while allowing you to secretly continue hearing all incoming audio.
 *
 * Usage:
 * 1. Press Ctrl + Shift + I in Discord to open Developer Tools Console.
 * 2. Paste this code and press Enter.
 * 3. Press F8 while in a voice call to toggle Fake Deafen ON/OFF.
 */

(() => {
    // 1. Verify Vencord is loaded
    if (typeof Vencord === "undefined" || !Vencord.Webpack) {
        console.error("%c[FakeDeafen] Vencord is not detected! Make sure Discord is running with Vencord.", "color: #ED4245; font-weight: bold;");
        return;
    }

    const { findByProps } = Vencord.Webpack;

    // 2. Locate Discord's internal modules
    const wsModule = findByProps("getSocket");
    const SelectedChannelStore = findByProps("getVoiceChannelId");
    const ChannelStore = findByProps("getChannel", "getDMFromUserId");
    const MediaEngineStore = findByProps("isDeaf", "isMute");

    let fakeDeafActive = false;

    // Audio feedback helper (subtle beep on toggle)
    function playChime(freq, duration = 0.12) {
        try {
            const ctx = new (window.AudioContext || window.webkitAudioContext)();
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();
            osc.type = "sine";
            osc.frequency.setValueAtTime(freq, ctx.currentTime);
            gain.gain.setValueAtTime(0.08, ctx.currentTime);
            gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + duration);
            osc.connect(gain);
            gain.connect(ctx.destination);
            osc.start();
            osc.stop(ctx.currentTime + duration);
        } catch (e) {}
    }

    // 3. Core Toggle Function
    function toggleFakeDeafen(forcedState) {
        const socket = wsModule?.getSocket();
        const channelId = SelectedChannelStore?.getVoiceChannelId();
        const channel = channelId ? ChannelStore?.getChannel(channelId) : null;

        if (!channelId || !socket) {
            console.warn("%c[FakeDeafen] You must be in a voice channel to toggle Fake Deafen!", "color: #FEE75C;");
            playChime(220, 0.2); // Low buzz
            return;
        }

        fakeDeafActive = typeof forcedState === "boolean" ? forcedState : !fakeDeafActive;

        try {
            // Discord Gateway Opcode 4: Voice State Update
            // Sends self_deaf: true to server, while leaving local audio playback untouched
            socket.send(4, {
                guild_id: channel?.guild_id ?? null,
                channel_id: channelId,
                self_mute: fakeDeafActive ? true : (MediaEngineStore?.isMute() ?? false),
                self_deaf: fakeDeafActive ? true : (MediaEngineStore?.isDeaf() ?? false),
                self_video: false,
                flags: 0
            });

            if (fakeDeafActive) {
                console.log(
                    "%c[FakeDeafen] ACTIVE 🎧 %c— You appear deafened to everyone, but you can still hear!",
                    "color: #57F287; font-weight: bold; font-size: 13px;",
                    "color: white;"
                );
                playChime(880, 0.12); // High chime (ON)
            } else {
                console.log(
                    "%c[FakeDeafen] INACTIVE 🔊 %c— Restored your normal voice state.",
                    "color: #ED4245; font-weight: bold; font-size: 13px;",
                    "color: white;"
                );
                playChime(440, 0.12); // Low chime (OFF)
            }
        } catch (err) {
            console.error("[FakeDeafen] Error updating voice state:", err);
        }
    }

    // Clean up previous event listeners if re-pasting
    if (window._fakeDeafenKeyHandler) {
        window.removeEventListener("keydown", window._fakeDeafenKeyHandler);
    }

    // 4. Register Hotkey (F8)
    window._fakeDeafenKeyHandler = (e) => {
        if (e.key === "F8" && !e.ctrlKey && !e.altKey) {
            e.preventDefault();
            toggleFakeDeafen();
        }
    };
    window.addEventListener("keydown", window._fakeDeafenKeyHandler);

    // Expose function globally for manual use in console
    window.toggleFakeDeafen = toggleFakeDeafen;

    console.log(
        "%c[FakeDeafen] Ready! Press F8 while in a voice call to toggle Fake Deafen.",
        "color: #5865F2; font-weight: bold; font-size: 14px;"
    );
})();
