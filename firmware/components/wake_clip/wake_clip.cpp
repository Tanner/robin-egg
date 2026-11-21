#include "wake_clip.h"

#include "esphome/core/hal.h"
#include "esphome/core/helpers.h"
#include "esphome/core/log.h"

#include <algorithm>
#include <cinttypes>
#include <cstdio>
#include <cstring>

namespace esphome::wake_clip {

static const char *const TAG = "wake_clip";

static constexpr uint32_t SAMPLE_RATE = 16000;
static constexpr uint16_t BITS_PER_SAMPLE = 16;
static constexpr uint16_t CHANNELS = 1;
static constexpr size_t WAV_HEADER_BYTES = 44;

// A freeze runs on the main loop, so it waits only briefly for a response that is still being
// sent. If it gives up, the wake is still counted, and the clip already frozen keeps its own
// count, so the server sees the mismatch and drops it instead of filing it under the new wake.
static constexpr TickType_t FREEZE_WAIT = pdMS_TO_TICKS(100);
static constexpr TickType_t RESPONSE_WAIT = pdMS_TO_TICKS(1000);
// Turning capture off runs on the main loop too, so it waits only briefly for a response that
// is still being sent. If it gives up, the response wipes the clip as soon as it has been sent.
static constexpr TickType_t DISABLE_WAIT = pdMS_TO_TICKS(200);

// The microphone sends audio every few tens of milliseconds while it runs. A longer gap means
// it was stopped and started again, so the audio before the gap is not joined to what follows.
static constexpr uint32_t RESTART_GAP_MS = 500;

static void put_le16(uint8_t *out, uint16_t value) {
  out[0] = value & 0xFF;
  out[1] = (value >> 8) & 0xFF;
}

static void put_le32(uint8_t *out, uint32_t value) {
  out[0] = value & 0xFF;
  out[1] = (value >> 8) & 0xFF;
  out[2] = (value >> 16) & 0xFF;
  out[3] = (value >> 24) & 0xFF;
}

float WakeClip::get_setup_priority() const {
  // After Wi-Fi is set up, so the HTTP server can bind, and before micro_wake_word
  // (AFTER_CONNECTION), so the data callback is in place before the microphone first starts.
  return setup_priority::AFTER_WIFI;
}

void WakeClip::setup() {
  RAMAllocator<uint8_t> psram(RAMAllocator<uint8_t>::ALLOC_EXTERNAL);
  this->ring_ = psram.allocate(this->capacity_bytes_);
  this->frozen_ = psram.allocate(WAV_HEADER_BYTES + this->capacity_bytes_);
  if (this->ring_ == nullptr || this->frozen_ == nullptr) {
    ESP_LOGE(TAG, "Could not allocate two buffers of %zu bytes in PSRAM, so no wake clips will be kept",
             this->capacity_bytes_);
    if (this->ring_ != nullptr) {
      psram.deallocate(this->ring_, this->capacity_bytes_);
      this->ring_ = nullptr;
    }
    if (this->frozen_ != nullptr) {
      psram.deallocate(this->frozen_, WAV_HEADER_BYTES + this->capacity_bytes_);
      this->frozen_ = nullptr;
    }
    this->mark_failed();
    return;
  }

  this->ring_lock_ = xSemaphoreCreateMutex();
  this->frozen_lock_ = xSemaphoreCreateMutex();
  if (this->ring_lock_ == nullptr || this->frozen_lock_ == nullptr) {
    ESP_LOGE(TAG, "Could not create the wake clip locks, so no wake clips will be kept");
    this->mark_failed();
    return;
  }

  if (!this->start_server_()) {
    this->mark_failed();
    return;
  }

  this->microphone_source_->add_data_callback([this](const std::vector<uint8_t> &data) { this->on_audio_(data); });
}

void WakeClip::dump_config() {
  ESP_LOGCONFIG(TAG,
                "Wake clip:\n"
                "  Seconds kept: %.1f\n"
                "  Port: %u\n"
                "  Model round: %s\n"
                "  Cutoff: %s\n"
                "  Microphone source passive: %s",
                this->capacity_bytes_ / (float) (SAMPLE_RATE * (BITS_PER_SAMPLE / 8) * CHANNELS), this->port_,
                this->model_round_.c_str(), this->cutoff_.c_str(),
                YESNO(this->microphone_source_->is_passive()));
}

void WakeClip::on_audio_(const std::vector<uint8_t> &data) {
  const uint8_t *src = data.data();
  size_t len = data.size();
  if (len == 0 || !this->enabled_.load()) {
    return;
  }
  if (len > this->capacity_bytes_) {
    src += len - this->capacity_bytes_;
    len = this->capacity_bytes_;
  }

  const uint32_t now = millis();
  xSemaphoreTake(this->ring_lock_, portMAX_DELAY);
  // Checked again under the lock: disable() empties the ring under it after turning capture off,
  // so a copy either finishes before that or sees capture off and writes nothing.
  if (!this->enabled_.load()) {
    xSemaphoreGive(this->ring_lock_);
    return;
  }
  if (now - this->last_audio_ms_ > RESTART_GAP_MS) {
    this->ring_write_ = 0;
    this->ring_filled_ = 0;
  }
  this->last_audio_ms_ = now;
  const size_t first = std::min(len, this->capacity_bytes_ - this->ring_write_);
  std::memcpy(this->ring_ + this->ring_write_, src, first);
  std::memcpy(this->ring_, src + first, len - first);
  this->ring_write_ = (this->ring_write_ + len) % this->capacity_bytes_;
  this->ring_filled_ = std::min(this->ring_filled_ + len, this->capacity_bytes_);
  xSemaphoreGive(this->ring_lock_);
}

void WakeClip::enable() {
  if (this->ring_lock_ == nullptr) {
    // Setup failed before the buffers and locks were made, so there is nothing to capture into.
    this->enabled_.store(true);
    return;
  }
  xSemaphoreTake(this->ring_lock_, portMAX_DELAY);
  this->ring_write_ = 0;
  this->ring_filled_ = 0;
  this->enabled_.store(true);
  xSemaphoreGive(this->ring_lock_);
  ESP_LOGI(TAG, "Wake clip capture is on, so the egg keeps the last %.1f s of audio before each wake",
           this->capacity_bytes_ / (float) (SAMPLE_RATE * (BITS_PER_SAMPLE / 8) * CHANNELS));
}

void WakeClip::disable() {
  this->enabled_.store(false);
  if (this->ring_lock_ == nullptr || this->frozen_lock_ == nullptr) {
    // The switch turns capture off at boot, before this component is set up, and a failed setup
    // leaves no locks. Either way no buffer holds audio yet.
    return;
  }

  xSemaphoreTake(this->ring_lock_, portMAX_DELAY);
  std::memset(this->ring_, 0, this->capacity_bytes_);
  this->ring_write_ = 0;
  this->ring_filled_ = 0;
  xSemaphoreGive(this->ring_lock_);

  // Set before waiting, so a response that ends while this waits wipes the clip itself.
  this->wipe_pending_.store(true);
  if (xSemaphoreTake(this->frozen_lock_, DISABLE_WAIT) != pdTRUE) {
    ESP_LOGW(TAG, "Wake clip capture is off and its buffer is empty. The frozen clip is still being sent, so it "
                  "will be wiped as soon as that response ends");
    return;
  }
  this->wipe_frozen_();
  xSemaphoreGive(this->frozen_lock_);
  ESP_LOGI(TAG, "Wake clip capture is off: emptied the buffer and wiped the frozen clip");
}

void WakeClip::wipe_frozen_() {
  std::memset(this->frozen_, 0, WAV_HEADER_BYTES + this->capacity_bytes_);
  this->frozen_data_bytes_ = 0;
  this->frozen_count_ = 0;
  this->frozen_wake_word_.clear();
  this->wipe_pending_.store(false);
}

void WakeClip::freeze(const std::string &wake_word) {
  if (!this->enabled_.load()) {
    return;
  }
  this->wake_count_++;
  if (this->is_failed()) {
    return;
  }

  if (xSemaphoreTake(this->frozen_lock_, FREEZE_WAIT) != pdTRUE) {
    ESP_LOGW(TAG, "Not freezing the clip for wake %" PRIu32 " (%s): the clip of an earlier wake is still being sent",
             this->wake_count_, wake_word.c_str());
    return;
  }
  if (this->wipe_pending_.load()) {
    // Capture was turned off while an earlier clip was being sent, and has been turned on again.
    this->wipe_frozen_();
  }

  xSemaphoreTake(this->ring_lock_, portMAX_DELAY);
  const size_t filled = this->ring_filled_;
  const size_t oldest = (this->ring_write_ + this->capacity_bytes_ - filled) % this->capacity_bytes_;
  const size_t first = std::min(filled, this->capacity_bytes_ - oldest);
  uint8_t *data = this->frozen_ + WAV_HEADER_BYTES;
  std::memcpy(data, this->ring_ + oldest, first);
  std::memcpy(data + first, this->ring_, filled - first);
  xSemaphoreGive(this->ring_lock_);

  this->frozen_data_bytes_ = filled;
  this->write_wav_header_(filled);
  this->frozen_count_ = this->wake_count_;
  this->frozen_wake_word_ = wake_word;
  xSemaphoreGive(this->frozen_lock_);

  ESP_LOGI(TAG, "Froze %.2f s of audio for wake %" PRIu32 " (%s)",
           filled / (float) (SAMPLE_RATE * (BITS_PER_SAMPLE / 8) * CHANNELS), this->wake_count_, wake_word.c_str());
}

void WakeClip::clear() {
  if (this->is_failed()) {
    return;
  }
  xSemaphoreTake(this->ring_lock_, portMAX_DELAY);
  this->ring_write_ = 0;
  this->ring_filled_ = 0;
  xSemaphoreGive(this->ring_lock_);
  ESP_LOGD(TAG, "Emptied the wake clip buffer, so the next clip holds only audio heard from now on");
}

void WakeClip::write_wav_header_(size_t data_bytes) {
  const uint32_t byte_rate = SAMPLE_RATE * CHANNELS * (BITS_PER_SAMPLE / 8);
  const uint16_t block_align = CHANNELS * (BITS_PER_SAMPLE / 8);
  uint8_t *h = this->frozen_;
  std::memcpy(h + 0, "RIFF", 4);
  put_le32(h + 4, 36 + data_bytes);
  std::memcpy(h + 8, "WAVE", 4);
  std::memcpy(h + 12, "fmt ", 4);
  put_le32(h + 16, 16);  // size of the PCM fmt chunk
  put_le16(h + 20, 1);   // PCM
  put_le16(h + 22, CHANNELS);
  put_le32(h + 24, SAMPLE_RATE);
  put_le32(h + 28, byte_rate);
  put_le16(h + 32, block_align);
  put_le16(h + 34, BITS_PER_SAMPLE);
  std::memcpy(h + 36, "data", 4);
  put_le32(h + 40, data_bytes);
}

bool WakeClip::start_server_() {
  httpd_config_t config = HTTPD_DEFAULT_CONFIG();
  config.server_port = this->port_;
  // Its own control port. ESPHome's web_server takes the default and Sendspin's WebSocket
  // server takes the default + 1, and a second server on a taken port never starts.
  config.ctrl_port = ESP_HTTPD_DEF_CTRL_PORT + 2;
  config.max_uri_handlers = 1;
  config.max_open_sockets = this->max_client_sockets_;
  config.lru_purge_enable = true;
  config.recv_wait_timeout = 3;
  config.send_wait_timeout = 3;

  esp_err_t err = httpd_start(&this->server_, &config);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "Could not start the wake clip HTTP server on port %u (%s), so no wake clips will be served",
             this->port_, esp_err_to_name(err));
    this->server_ = nullptr;
    return false;
  }

  const httpd_uri_t wake_clip_uri = {
      .uri = "/wake-clip",
      .method = HTTP_GET,
      .handler = WakeClip::handle_wake_clip_,
      .user_ctx = this,
  };
  err = httpd_register_uri_handler(this->server_, &wake_clip_uri);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "Could not register GET /wake-clip on the wake clip HTTP server (%s)", esp_err_to_name(err));
    httpd_stop(this->server_);
    this->server_ = nullptr;
    return false;
  }
  return true;
}

esp_err_t WakeClip::handle_wake_clip_(httpd_req_t *req) {
  auto *self = static_cast<WakeClip *>(req->user_ctx);

  if (!self->enabled_.load()) {
    return httpd_resp_send_err(req, HTTPD_404_NOT_FOUND, "Wake clip capture is off");
  }

  if (xSemaphoreTake(self->frozen_lock_, RESPONSE_WAIT) != pdTRUE) {
    ESP_LOGW(TAG, "Answering a wake clip request with 503: a freeze held the clip for too long");
    httpd_resp_set_status(req, "503 Service Unavailable");
    return httpd_resp_send(req, nullptr, 0);
  }

  if (self->wipe_pending_.load()) {
    self->wipe_frozen_();
  }
  // Checked again under the lock: capture may have been turned off while this waited for it.
  if (!self->enabled_.load()) {
    xSemaphoreGive(self->frozen_lock_);
    return httpd_resp_send_err(req, HTTPD_404_NOT_FOUND, "Wake clip capture is off");
  }
  if (self->frozen_count_ == 0) {
    xSemaphoreGive(self->frozen_lock_);
    return httpd_resp_send_err(req, HTTPD_404_NOT_FOUND, "No wake clip has been frozen since capture was turned on");
  }

  // httpd keeps pointers to these until the response is sent, which is before the lock is given back.
  char count[12];
  snprintf(count, sizeof(count), "%" PRIu32, self->frozen_count_);
  httpd_resp_set_type(req, "audio/wav");
  httpd_resp_set_hdr(req, "X-Wake-Count", count);
  httpd_resp_set_hdr(req, "X-Wake-Word", self->frozen_wake_word_.c_str());
  httpd_resp_set_hdr(req, "X-Model-Round", self->model_round_.c_str());
  httpd_resp_set_hdr(req, "X-Cutoff", self->cutoff_.c_str());
  const size_t total = WAV_HEADER_BYTES + self->frozen_data_bytes_;
  const esp_err_t err = httpd_resp_send(req, reinterpret_cast<const char *>(self->frozen_), total);
  const uint32_t sent_count = self->frozen_count_;
  const bool wiped = self->wipe_pending_.load();
  if (wiped) {
    self->wipe_frozen_();
  }
  xSemaphoreGive(self->frozen_lock_);
  if (wiped) {
    ESP_LOGI(TAG, "Wiped the clip for wake %" PRIu32 " once it was sent, because wake clip capture was turned off",
             sent_count);
  }

  if (err != ESP_OK) {
    ESP_LOGW(TAG, "Could not send the clip for wake %" PRIu32 " (%s)", sent_count, esp_err_to_name(err));
    return ESP_FAIL;
  }
  ESP_LOGD(TAG, "Sent the clip for wake %" PRIu32 ", %zu bytes", sent_count, total);
  return ESP_OK;
}

}  // namespace esphome::wake_clip
