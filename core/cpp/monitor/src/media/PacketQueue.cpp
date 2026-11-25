#include "media/PacketQueue.hpp"

#include <QMutexLocker>

PacketQueue::PacketQueue(std::size_t maxPackets) : m_maxPackets(maxPackets) {}

PacketQueue::~PacketQueue() { clear(); }

bool PacketQueue::push(const AVPacket *packet) {
  if (!packet)
    return false;

  PacketPtr copy(av_packet_alloc(), [](AVPacket *p) {
    if (p)
      av_packet_free(&p);
  });

  if (!copy || av_packet_ref(copy.get(), packet) < 0)
    return false;

  QMutexLocker locker(&m_mutex);

  while (m_maxPackets > 0 && m_queue.size() >= m_maxPackets && !m_aborted &&
         !m_finished) {
    m_condition.wait(&m_mutex, 100);
  }

  if (m_aborted || m_finished)
    return false;

  m_queue.push_back(std::move(copy));
  m_condition.wakeOne();
  return true;
}

PacketQueue::PopResult PacketQueue::pop(PacketPtr &packet) {
  QMutexLocker locker(&m_mutex);

  while (m_queue.empty() && !m_finished && !m_aborted && !m_resetRequested)
    m_condition.wait(&m_mutex);

  if (m_aborted) {
    packet.reset();
    return PopResult::Aborted;
  }
  if (m_resetRequested) {
    m_resetRequested = false;
    packet.reset();
    return PopResult::Reset;
  }
  if (!m_queue.empty()) {
    packet = std::move(m_queue.front());
    m_queue.pop_front();
    m_condition.wakeOne();
    return PopResult::Packet;
  }

  packet.reset();
  return PopResult::End;
}

void PacketQueue::finish() {
  QMutexLocker locker(&m_mutex);
  if (!m_aborted)
    m_finished = true;
  m_condition.wakeAll();
}

void PacketQueue::abort() {
  QMutexLocker locker(&m_mutex);
  m_aborted = true;
  m_condition.wakeAll();
}

void PacketQueue::reset() {
  QMutexLocker locker(&m_mutex);
  m_queue.clear();
  m_finished = m_aborted = m_resetRequested = false;
}

void PacketQueue::resetForLoop() {
  QMutexLocker locker(&m_mutex);
  m_queue.clear();
  m_finished = false;
  m_resetRequested = true;
  m_condition.wakeAll();
}

void PacketQueue::clear() {
  QMutexLocker locker(&m_mutex);
  m_queue.clear();
}

std::size_t PacketQueue::size() const {
  QMutexLocker locker(&m_mutex);
  return m_queue.size();
}

bool PacketQueue::isAborted() const {
  QMutexLocker locker(&m_mutex);
  return m_aborted;
}
