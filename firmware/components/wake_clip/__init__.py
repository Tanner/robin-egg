"""Keeps the last few seconds of what the wake word model hears and serves a frozen copy over HTTP.

Capture is off at boot. wake_clip.enable turns it on; wake_clip.disable turns it off and wipes
both buffers.

The audio comes from a passive microphone source: the component only hears the microphone while
micro_wake_word or voice_assistant has it running, and never starts, stops or reconfigures it.
"""

from esphome import automation
import esphome.codegen as cg
from esphome.components import microphone, socket
from esphome.components.esp32 import include_builtin_idf_component
import esphome.config_validation as cv
from esphome.const import CONF_ID, CONF_MICROPHONE, CONF_PORT, CONF_SECONDS

AUTO_LOAD = ["socket"]
DEPENDENCIES = ["microphone", "psram"]

CONF_CUTOFF = "cutoff"
CONF_MODEL_ROUND = "model_round"
CONF_WAKE_WORD = "wake_word"

# The wake word model hears 16 kHz audio, so the clip is kept at that rate.
SAMPLE_RATE = 16000

# The HTTP server's client connections. wake_clip.cpp sets httpd's max_open_sockets from
# this, so the firmware's socket pool always holds every socket the server can open.
MAX_CLIENT_SOCKETS = 2

wake_clip_ns = cg.esphome_ns.namespace("wake_clip")
WakeClip = wake_clip_ns.class_("WakeClip", cg.Component)

CONFIG_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(WakeClip),
            cv.Optional(
                CONF_MICROPHONE, default={}
            ): microphone.microphone_source_schema(
                min_bits_per_sample=16,
                max_bits_per_sample=16,
                min_channels=1,
                max_channels=1,
            ),
            cv.Optional(CONF_SECONDS, default=5): cv.int_range(min=1, max=30),
            cv.Optional(CONF_PORT, default=8080): cv.port,
            cv.Required(CONF_MODEL_ROUND): cv.string_strict,
            cv.Required(CONF_CUTOFF): cv.string,
        }
    ).extend(cv.COMPONENT_SCHEMA),
    cv.only_on_esp32,
    # Counted in CONFIG_LWIP_MAX_SOCKETS: the clients, the listening socket, and the UDP
    # control socket ESP-IDF's httpd keeps open for as long as it runs.
    socket.consume_sockets(MAX_CLIENT_SOCKETS, "wake_clip"),
    socket.consume_sockets(1, "wake_clip", socket.SocketType.TCP_LISTEN),
    socket.consume_sockets(1, "wake_clip", socket.SocketType.UDP),
)

FINAL_VALIDATE_SCHEMA = cv.Schema(
    {
        cv.Required(
            CONF_MICROPHONE
        ): microphone.final_validate_microphone_source_schema(
            "wake_clip", sample_rate=SAMPLE_RATE
        ),
    },
    extra=cv.ALLOW_EXTRA,
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)

    mic_source = await microphone.microphone_source_to_code(
        config[CONF_MICROPHONE], passive=True
    )
    cg.add(var.set_microphone_source(mic_source))
    cg.add(var.set_capacity_bytes(config[CONF_SECONDS] * SAMPLE_RATE * 2))
    cg.add(var.set_port(config[CONF_PORT]))
    cg.add(var.set_max_client_sockets(MAX_CLIENT_SOCKETS))
    cg.add(var.set_model_round(config[CONF_MODEL_ROUND]))
    cg.add(var.set_cutoff(config[CONF_CUTOFF]))

    include_builtin_idf_component("esp_http_server")


automation.register_apply_action(
    "wake_clip.freeze",
    cv.Schema(
        {
            cv.GenerateID(): cv.use_id(WakeClip),
            cv.Required(CONF_WAKE_WORD): cv.templatable(cv.string),
        }
    ),
    automation.ApplyField(CONF_WAKE_WORD, "freeze", cg.std_string),
)

automation.register_apply_action(
    "wake_clip.clear",
    automation.maybe_simple_id(
        {
            cv.GenerateID(): cv.use_id(WakeClip),
        }
    ),
    automation.ApplyCall("clear()"),
)

automation.register_apply_action(
    "wake_clip.enable",
    automation.maybe_simple_id(
        {
            cv.GenerateID(): cv.use_id(WakeClip),
        }
    ),
    automation.ApplyCall("enable()"),
)

automation.register_apply_action(
    "wake_clip.disable",
    automation.maybe_simple_id(
        {
            cv.GenerateID(): cv.use_id(WakeClip),
        }
    ),
    automation.ApplyCall("disable()"),
)
