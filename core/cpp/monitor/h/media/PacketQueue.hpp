#pragma once

extern "C" {
#include <libavcodec/packet.h>
}

#include <QMutex>
#include <QWaitCondition>
#include <cstddef>
#include <deque>
#include <memory>

class PacketQueue {
public:
  using PacketPtr = std::shared_ptr<AVPacket>;

  enum class PopResult { Packet, End, Aborted, Reset };

  explicit PacketQueue(std::size_t maxPackets = 256);
  ~PacketQueue();

  bool push(const AVPacket *packet);
  PopResult pop(PacketPtr &packet);

  void finish();
  void abort();
  void reset();
  void resetForLoop();
  void clear();
  bool isResetRequested() const { return m_resetRequested; }

  std::size_t size() const;
  bool isAborted() const;

private:
  mutable QMutex m_mutex;
  QWaitCondition m_condition;
  std::deque<PacketPtr> m_queue;

  std::size_t m_maxPackets = 256;
  bool m_finished = false;
  bool m_aborted = false;
  bool m_resetRequested = false;
};
