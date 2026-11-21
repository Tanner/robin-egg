#pragma once

#include "esphome/components/microphone/microphone_source.h"
#include "esphome/core/component.h"

#include <esp_http_server.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace esphome::wake_clip {

/// Keeps the last few seconds of 16 kHz mono 16-bit audio in a ring buffer and, on each wake,
/// freezes a copy that an HTTP server on the egg serves as a WAV at GET /wake-clip.
///
/// Three tasks touch the buffers:
///   - the microphone's audio task writes the ring buffer, holding ring_lock_ only for the copy;
///   - the main loop freezes, holding frozen_lock_ and then ring_lock_ while it copies;
///   - the HTTP server task sends the frozen buffer, holding frozen_lock_ for the whole response.
/// The audio task never takes frozen_lock_, so a slow response can never hold up the microphone.
///
/// Capture is off until enable() and after disable(). While it is off the ring buffer is not
/// written, a wake freezes nothing and GET /wake-clip answers 404.
class WakeClip : public Component {
 public:
  void setup() override;
  void dump_config() override;
  float get_setup_priority() const override;

  void set_microphone_source(microphone::MicrophoneSource *source) { this->microphone_source_ = source; }
  void set_capacity_bytes(size_t capacity_bytes) { this->capacity_bytes_ = capacity_bytes; }
  void set_port(uint16_t port) { this->port_ = port; }
  void set_max_client_sockets(uint8_t max_client_sockets) { this->max_client_sockets_ = max_client_sockets; }
  void set_model_round(const std::string &model_round) { this->model_round_ = model_round; }
  void set_cutoff(const std::string &cutoff) { this->cutoff_ = cutoff; }

  /// Turns capture on, starting from an empty ring buffer. Runs on the main loop.
  void enable();

  /// Turns capture off, then zeroes the ring buffer and wipes the frozen clip, so no audio heard
  /// before stays in memory. The wake count is kept, so counts never repeat within one boot.
  /// Runs on the main loop.
  void disable();

  bool is_enabled() const { return this->enabled_.load(); }

  /// Copies the ring buffer into the frozen buffer, oldest audio first, and counts the wake.
  /// Does nothing while capture is off. Runs on the main loop.
  void freeze(const std::string &wake_word);

  /// Empties the ring buffer, so the next clip holds only audio heard from now on. The frozen
  /// clip is left as it is. Runs on the main loop.
  void clear();

  /// The number of wakes since boot, frozen or not.
  uint32_t get_count() const { return this->wake_count_; }

 protected:
  void on_audio_(const std::vector<uint8_t> &data);
  bool start_server_();
  static esp_err_t handle_wake_clip_(httpd_req_t *req);
  void write_wav_header_(size_t data_bytes);
  /// Zeroes the frozen clip and forgets its wake. The caller holds frozen_lock_.
  void wipe_frozen_();

  microphone::MicrophoneSource *microphone_source_{nullptr};
  size_t capacity_bytes_{0};
  uint16_t port_{8080};
  uint8_t max_client_sockets_{2};
  std::string model_round_;
  std::string cutoff_;

  // The ring buffer. ring_write_ is where the next byte goes; ring_filled_ stops growing at
  // capacity_bytes_. last_audio_ms_ is when audio last arrived, to tell a microphone that
  // stopped and started again. All four are guarded by ring_lock_.
  uint8_t *ring_{nullptr};
  size_t ring_write_{0};
  size_t ring_filled_{0};
  uint32_t last_audio_ms_{0};
  SemaphoreHandle_t ring_lock_{nullptr};

  // The frozen clip: a 44-byte WAV header followed by up to capacity_bytes_ of audio, so a
  // response is one contiguous buffer. Guarded by frozen_lock_, with the wake word and count.
  uint8_t *frozen_{nullptr};
  size_t frozen_data_bytes_{0};
  uint32_t frozen_count_{0};
  std::string frozen_wake_word_;
  SemaphoreHandle_t frozen_lock_{nullptr};

  // Only touched on the main loop.
  uint32_t wake_count_{0};

  // Read by all three tasks, written only on the main loop. Atomic, so a reader never needs a
  // lock just to see it; the writes that matter are also ordered by the locks: on_audio_ reads
  // it under ring_lock_, and the HTTP handler reads it under frozen_lock_.
  std::atomic<bool> enabled_{false};
  // Set by disable() before it waits for frozen_lock_. Whoever next holds frozen_lock_ (disable
  // itself, a response that was being sent, or a freeze) wipes the frozen clip and clears it.
  std::atomic<bool> wipe_pending_{false};

  httpd_handle_t server_{nullptr};
};

}  // namespace esphome::wake_clip
