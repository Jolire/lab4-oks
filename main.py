import sys
import random
import time
from typing import List, Optional, Dict
from enum import Enum
from dataclasses import dataclass
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QGraphicsScene, QGraphicsView,
    QPushButton, QVBoxLayout, QHBoxLayout, QWidget, QGraphicsEllipseItem,
    QGraphicsTextItem, QLineEdit, QLabel, QTextEdit, QFormLayout, QGroupBox,
    QComboBox, QCheckBox, QGraphicsLineItem, QGraphicsSimpleTextItem,
    QDialog, QListWidget, QTabWidget, QSpinBox, QProgressBar
)
from PyQt6.QtGui import QColor, QBrush, QPen, QFont, QPainter
from PyQt6.QtCore import Qt, QTimer, QPointF, QRectF


# ==================== Packet Module ====================

class PacketType(Enum):
    RTS = "RTS"
    CTS = "CTS"
    DATA = "DATA"
    ACK = "ACK"


@dataclass
class Packet:
    packet_type: PacketType
    sender_id: int
    receiver_id: int
    data: str = ""
    duration: int = 0
    message_id: int = 0

    def __str__(self):
        if self.packet_type == PacketType.RTS:
            return f"RTS: {self.sender_id}→{self.receiver_id} (dur={self.duration})"
        elif self.packet_type == PacketType.CTS:
            return f"CTS: {self.receiver_id}→{self.sender_id} (dur={self.duration})"
        elif self.packet_type == PacketType.DATA:
            return f"DATA: {self.sender_id}→{self.receiver_id} [{self.data}]"
        elif self.packet_type == PacketType.ACK:
            return f"ACK: {self.receiver_id}→{self.sender_id}"
        return ""


# ==================== Station Module ====================

class StationState(Enum):
    IDLE = "Ожидание"
    SENSING = "Прослушивание канала"
    SENDING_RTS = "Отправка RTS"
    WAITING_CTS = "Ожидание CTS"
    SENDING_DATA = "Передача данных"
    WAITING_ACK = "Ожидание ACK"
    RECEIVING = "Прием данных"
    BACKOFF = "Случайная задержка"
    ERROR = "Ошибка"


@dataclass
class Message:
    sender_id: int
    receiver_id: int
    data: str
    message_id: int


class Station:
    def __init__(self, station_id: int, x: float, y: float):
        self.id = station_id
        self.x = x
        self.y = y
        self.state = StationState.IDLE
        self.message_queue = []
        self.current_message: Optional[Message] = None
        self.backoff_timer = 0
        self.timeout_timer = 0
        self.has_error = False
        self.waiting_for_cts_from = None
        self.reserved_for = None
        self.retry_counter = 0
        self.nav = 0  # Network Allocation Vector
        self.difs_timer = 0
        self.transmission_history = []  # История передач для статистики

    def add_message(self, receiver_id: int, data: str, message_id: int):
        msg = Message(self.id, receiver_id, data, message_id)
        self.message_queue.append(msg)

    def set_error(self, error: bool):
        self.has_error = error

    def add_transmission_record(self, packet_type: PacketType, success: bool):
        self.transmission_history.append({
            'type': packet_type,
            'success': success,
            'time': time.time()
        })


# ==================== Protocol Module ====================

class CSMACAProtocol:
    DIFS = 50
    SIFS = 10
    RTS_TIME = 20
    CTS_TIME = 20
    DATA_TIME = 100
    ACK_TIME = 20
    TIMEOUT = 200
    SLOT_TIME = 20
    CW_MIN = 4
    CW_MAX = 32
    MAX_RETRIES = 10

    def __init__(self):
        self.stations: List[Station] = []
        self.channel_busy = False
        self.current_transmission: Optional[Packet] = None
        self.transmission_timer = 0
        self.step_counter = 0
        self.last_collision_stations: List[Station] = []
        self.total_collisions = 0
        self.successful_transmissions = 0
        self.failed_transmissions = 0
        self.channel_utilization = 0
        self.active_time = 0

    def add_station(self, x: float, y: float) -> Station:
        station_id = len(self.stations) + 1
        existing_ids = {s.id for s in self.stations}
        while station_id in existing_ids:
            station_id += 1
        station = Station(station_id, x, y)
        self.stations.append(station)
        return station

    def remove_station(self, station_id: int):
        self.stations = [s for s in self.stations if s.id != station_id]

    def get_station(self, station_id: int) -> Optional[Station]:
        for s in self.stations:
            if s.id == station_id:
                return s
        return None

    def is_channel_idle(self) -> bool:
        return not self.channel_busy

    def process_step(self) -> List[str]:
        logs = []
        self.step_counter += 1
        self.last_collision_stations.clear()

        # Обновление статистики использования канала
        if self.channel_busy:
            self.active_time += 1
        self.channel_utilization = self.active_time / self.step_counter

        for station in self.stations:
            if station.nav > 0:
                station.nav -= 1

        if self.transmission_timer > 0:
            self.transmission_timer -= 1
            if self.transmission_timer == 0:
                logs.extend(self._complete_transmission())

        contention_winners = []

        for station in self.stations:
            if station.timeout_timer > 0:
                station.timeout_timer -= 1
                if station.timeout_timer == 0:
                    logs.append(f"[Станция {station.id}] Таймаут истек")
                    logs.extend(self._handle_timeout(station))

            if station.state == StationState.SENSING:
                if self.is_channel_idle() and station.nav == 0:
                    station.difs_timer -= 1
                    if station.difs_timer == 0:
                        logs.extend(self._start_initial_backoff(station))
                else:
                    station.state = StationState.IDLE
                    station.difs_timer = 0
                    logs.append(f"[Станция {station.id}] Канал занят во время DIFS, отмена")

            elif station.state == StationState.BACKOFF:
                if station.backoff_timer == 0:
                    contention_winners.append(station)

                elif self.is_channel_idle() and station.nav == 0:
                    station.backoff_timer -= 1
                    if station.backoff_timer == 0:
                        contention_winners.append(station)

            elif station.state == StationState.IDLE and len(station.message_queue) > 0:
                if self.is_channel_idle() and station.nav == 0:
                    logs.extend(self._initiate_transmission(station))

        if contention_winners:
            logs.extend(self._handle_contention_resolution(contention_winners))

        return logs

    def _handle_contention_resolution(self, winners: List[Station]) -> List[str]:
        logs = []
        if self.is_channel_idle():
            if len(winners) == 1:
                winner = winners[0]
                logs.append(f"[Станция {winner.id}] Выиграла конкуренцию")
                logs.extend(self._send_rts(winner))
            else:
                ids = ", ".join(str(s.id) for s in winners)
                logs.append(f"[КОЛЛИЗИЯ] Станции {ids} пытаются передавать одновременно")
                self.last_collision_stations = winners
                self.total_collisions += 1
                for station in winners:
                    station.add_transmission_record(PacketType.RTS, False)
                    logs.extend(self._enter_backoff(station, is_collision=True))
        else:
            ids = ", ".join(str(s.id) for s in winners)
            logs.append(f"[Станция(и) {ids}] Backoff истек, но канал уже занят. Повтор.")
            for station in winners:
                logs.extend(self._enter_backoff(station, is_collision=False))
        return logs

    def _initiate_transmission(self, station: Station) -> List[str]:
        station.current_message = station.message_queue[0]
        station.state = StationState.SENSING
        station.difs_timer = self.DIFS
        return [f"[Станция {station.id}] Прослушивание канала (DIFS)"]

    def _start_initial_backoff(self, station: Station) -> List[str]:
        station.retry_counter = 0
        backoff_slots = random.randint(0, self.CW_MIN - 1)
        station.backoff_timer = backoff_slots * self.SLOT_TIME
        station.state = StationState.BACKOFF

        logs = [f"[Станция {station.id}] DIFS истек, начало Backoff: {station.backoff_timer} единиц"]
        return logs

    def _send_rts(self, station: Station) -> List[str]:
        msg = station.current_message
        duration = self.CTS_TIME + self.SIFS + self.DATA_TIME + self.SIFS + self.ACK_TIME
        packet = Packet(PacketType.RTS, station.id, msg.receiver_id, duration=duration, message_id=msg.message_id)

        station.state = StationState.SENDING_RTS
        self.channel_busy = True
        self.current_transmission = packet
        self.transmission_timer = self.RTS_TIME

        return [f"[Станция {station.id}] → RTS → Станция {msg.receiver_id} (duration={duration})"]

    def _enter_backoff(self, station: Station, is_collision: bool) -> List[str]:
        random.seed()
        if is_collision:
            station.retry_counter += 1

        cw_exponent = min(station.retry_counter, 5)
        cw = min(self.CW_MIN * (2 ** cw_exponent), self.CW_MAX)
        backoff_slots = random.randint(0, cw - 1)
        backoff_time = backoff_slots * self.SLOT_TIME

        station.backoff_timer = backoff_time
        station.state = StationState.BACKOFF
        station.timeout_timer = 0
        station.waiting_for_cts_from = None

        log_msg = "Коллизия" if is_collision else "Повтор"
        return [f"[Станция {station.id}] {log_msg}. Backoff: {backoff_time} (попытка {station.retry_counter + 1})"]

    def _handle_timeout(self, station: Station) -> List[str]:
        logs = []
        intended_receiver_id = None

        if station.state == StationState.WAITING_CTS:
            logs.append(f"[Станция {station.id}] CTS не получен, повторная попытка")
            if station.current_message:
                intended_receiver_id = station.current_message.receiver_id
        elif station.state == StationState.WAITING_ACK:
            logs.append(f"[Станция {station.id}] ACK не получен, повторная попытка")
            if station.current_message:
                intended_receiver_id = station.current_message.receiver_id

        if station.retry_counter >= self.MAX_RETRIES - 1:
            if intended_receiver_id:
                intended_receiver = self.get_station(intended_receiver_id)
                if intended_receiver and intended_receiver.reserved_for == station.id:
                    intended_receiver.state = StationState.IDLE
                    intended_receiver.reserved_for = None
                    logs.append(
                        f"[Станция {intended_receiver_id}] Сброс состояния после отказа отправителя"
                    )

            if station.current_message:
                failed_msg_id = station.current_message.message_id
                self.failed_transmissions += 1
                logs.append(
                    f"[Станция {station.id}] ПРЕДЕЛ ПОПЫТОК ДОСТИГНУТ. "
                    f"Сообщение #{failed_msg_id} помечено как недоставленное и удалено"
                )
                if len(station.message_queue) > 0 and station.message_queue[0].message_id == failed_msg_id:
                    station.message_queue.pop(0)

            station.current_message = None
            station.state = StationState.IDLE
            station.timeout_timer = 0
            station.waiting_for_cts_from = None
            station.has_error = False
            station.retry_counter = 0

            return logs

        if intended_receiver_id:
            intended_receiver = self.get_station(intended_receiver_id)
            if intended_receiver and intended_receiver.reserved_for == station.id:
                intended_receiver.state = StationState.IDLE
                intended_receiver.reserved_for = None
                logs.append(f"[Станция {intended_receiver_id}] Сброс состояния после таймаута отправителя")

        logs.extend(self._enter_backoff(station, is_collision=True))
        return logs

    def _complete_transmission(self) -> List[str]:
        logs = []
        if self.current_transmission is None:
            self.channel_busy = False
            return logs
        packet = self.current_transmission
        original_packet = packet
        if packet.packet_type == PacketType.RTS:
            logs.extend(self._handle_rts_received(packet))
        elif packet.packet_type == PacketType.CTS:
            logs.extend(self._handle_cts_received(packet))
        elif packet.packet_type == PacketType.DATA:
            logs.extend(self._handle_data_received(packet))
        elif packet.packet_type == PacketType.ACK:
            logs.extend(self._handle_ack_received(packet))
        if self.current_transmission is original_packet:
            self.current_transmission = None
            self.channel_busy = False
        return logs

    def _handle_rts_received(self, packet: Packet) -> List[str]:
        logs = []
        sender = self.get_station(packet.sender_id)
        receiver = self.get_station(packet.receiver_id)
        if sender is None or receiver is None:
            return logs
        logs.append(f"[Станция {packet.receiver_id}] ← RTS получен от станции {packet.sender_id}")
        for station in self.stations:
            if station.id != packet.sender_id and station.id != packet.receiver_id:
                station.nav = packet.duration
                logs.append(f"[Станция {station.id}] NAV установлен на {packet.duration}")
        sender.state = StationState.WAITING_CTS
        sender.timeout_timer = self.TIMEOUT
        sender.waiting_for_cts_from = packet.receiver_id
        if receiver.state == StationState.IDLE or receiver.state == StationState.SENSING:
            receiver.state = StationState.RECEIVING
            receiver.reserved_for = packet.sender_id
            cts_packet = Packet(PacketType.CTS, packet.receiver_id, packet.sender_id,
                                duration=packet.duration - self.CTS_TIME - self.SIFS, message_id=packet.message_id)
            self.current_transmission = cts_packet
            self.transmission_timer = self.SIFS + self.CTS_TIME
            self.channel_busy = True
            logs.append(f"[Станция {packet.receiver_id}] → CTS → Станция {packet.sender_id} (после SIFS)")
        else:
            logs.append(f"[Станция {packet.receiver_id}] Занята, CTS не отправлен")
        return logs

    def _handle_cts_received(self, packet: Packet) -> List[str]:
        logs = []
        sender = self.get_station(packet.sender_id)
        receiver = self.get_station(packet.receiver_id)
        if sender is None or receiver is None:
            return logs
        logs.append(f"[Станция {packet.receiver_id}] ← CTS получен от станции {packet.sender_id}")
        for station in self.stations:
            if station.id != packet.sender_id and station.id != packet.receiver_id:
                if station.nav < packet.duration:
                    station.nav = packet.duration
        if receiver.state == StationState.WAITING_CTS and receiver.waiting_for_cts_from == packet.sender_id:
            receiver.timeout_timer = 0
            receiver.state = StationState.SENDING_DATA
            msg = receiver.current_message
            data_packet = Packet(PacketType.DATA, receiver.id, packet.sender_id, data=msg.data,
                                 message_id=msg.message_id)
            if receiver.has_error:
                data_packet.data = "[ОШИБКА_ДАННЫХ]"
                logs.append(f"[Станция {receiver.id}] ОШИБКА: данные повреждены")
            self.current_transmission = data_packet
            self.transmission_timer = self.SIFS + self.DATA_TIME
            self.channel_busy = True
            logs.append(f"[Станция {receiver.id}] → DATA → Станция {packet.sender_id} (после SIFS)")
        return logs

    def _handle_data_received(self, packet: Packet) -> List[str]:
        logs = []
        sender = self.get_station(packet.sender_id)
        receiver = self.get_station(packet.receiver_id)
        if sender is None or receiver is None:
            return logs
        logs.append(f"[Станция {packet.receiver_id}] ← DATA получены от станции {packet.sender_id}: '{packet.data}'")
        sender.state = StationState.WAITING_ACK
        sender.timeout_timer = self.TIMEOUT
        if receiver.has_error:
            logs.append(f"[Станция {packet.receiver_id}] ОШИБКА ПРИЕМА: станция неисправна, ACK не отправлен")
            receiver.state = StationState.IDLE
            receiver.reserved_for = None
            self.failed_transmissions += 1
            sender.add_transmission_record(PacketType.DATA, False)
            return logs
        if "[ОШИБКА_ДАННЫХ]" in packet.data:
            logs.append(f"[Станция {packet.receiver_id}] Обнаружена ошибка в данных, ACK не отправлен")
            receiver.state = StationState.IDLE
            receiver.reserved_for = None
            self.failed_transmissions += 1
            sender.add_transmission_record(PacketType.DATA, False)
            return logs
        ack_packet = Packet(PacketType.ACK, packet.receiver_id, packet.sender_id, message_id=packet.message_id)
        self.current_transmission = ack_packet
        self.transmission_timer = self.SIFS + self.ACK_TIME
        self.channel_busy = True
        logs.append(f"[Станция {packet.receiver_id}] → ACK → Станция {packet.sender_id} (после SIFS)")
        receiver.state = StationState.IDLE
        receiver.reserved_for = None
        return logs

    def _handle_ack_received(self, packet: Packet) -> List[str]:
        logs = []
        sender = self.get_station(packet.sender_id)
        receiver = self.get_station(packet.receiver_id)
        if sender is None or receiver is None:
            return logs
        logs.append(f"[Станция {packet.receiver_id}] ← ACK получен от станции {packet.sender_id}")
        if receiver.state == StationState.WAITING_ACK:
            self.successful_transmissions += 1
            receiver.add_transmission_record(PacketType.DATA, True)
            receiver.timeout_timer = 0
            if len(receiver.message_queue) > 0 and receiver.message_queue[0].message_id == packet.message_id:
                completed_msg = receiver.message_queue.pop(0)
                logs.append(f"[Станция {receiver.id}] Сообщение #{completed_msg.message_id} успешно доставлено")
            receiver.current_message = None
            receiver.state = StationState.IDLE
            receiver.has_error = False
            receiver.retry_counter = 0
        return logs

    def get_statistics(self) -> dict:
        """Возвращает статистику работы сети"""
        return {
            'total_stations': len(self.stations),
            'total_steps': self.step_counter,
            'successful_transmissions': self.successful_transmissions,
            'failed_transmissions': self.failed_transmissions,
            'total_collisions': self.total_collisions,
            'channel_utilization': f"{self.channel_utilization:.2%}",
            'total_messages': sum(len(s.message_queue) for s in self.stations)
        }


# ==================== GUI Module ====================

# Словарь для сопоставления состояний станций с цветами
STATE_COLORS = {
    StationState.IDLE: QColor("lightblue"),
    StationState.SENSING: QColor("lightyellow"),
    StationState.SENDING_RTS: QColor("orange"),
    StationState.WAITING_CTS: QColor("yellow"),
    StationState.SENDING_DATA: QColor("red"),
    StationState.WAITING_ACK: QColor("pink"),
    StationState.RECEIVING: QColor("lightgreen"),
    StationState.BACKOFF: QColor("lightgray"),
    StationState.ERROR: QColor("darkred"),
}

# Словарь для сопоставления типов пакетов с цветами и стилями линий
PACKET_LINE_STYLES = {
    PacketType.RTS: {"color": QColor("orange"), "style": Qt.PenStyle.DashLine, "width": 3},
    PacketType.CTS: {"color": QColor("gold"), "style": Qt.PenStyle.DashLine, "width": 3},
    PacketType.DATA: {"color": QColor("red"), "style": Qt.PenStyle.SolidLine, "width": 4},
    PacketType.ACK: {"color": QColor("green"), "style": Qt.PenStyle.DotLine, "width": 3},
}


class ChannelStatusWidget(QGraphicsEllipseItem):
    """Виджет для отображения состояния канала"""

    def __init__(self, x, y, protocol):
        super().__init__(0, 0, 80, 80)
        self.setPos(x, y)
        self.protocol = protocol
        self.setBrush(QBrush(QColor(240, 240, 240)))
        self.setPen(QPen(Qt.GlobalColor.black, 2))

        self.status_text = QGraphicsTextItem("Канал", self)
        self.status_text.setDefaultTextColor(QColor("black"))
        font = QFont()
        font.setBold(True)
        self.status_text.setFont(font)
        self.status_text.setPos(15, 15)

        self.state_text = QGraphicsTextItem("Свободен", self)
        self.state_text.setDefaultTextColor(QColor("green"))
        self.state_text.setPos(10, 45)

    def update_status(self):
        if self.protocol.channel_busy:
            self.setBrush(QBrush(QColor(255, 200, 200)))
            self.state_text.setPlainText("Занят")
            self.state_text.setDefaultTextColor(QColor("red"))
        else:
            self.setBrush(QBrush(QColor(200, 255, 200)))
            self.state_text.setPlainText("Свободен")
            self.state_text.setDefaultTextColor(QColor("green"))


class MessageQueueDialog(QDialog):
    """Отдельное окно для отображения очереди сообщений и состояния станции."""

    def __init__(self, station: Station, parent=None):
        super().__init__(parent)
        self.station = station
        self.setWindowTitle(f"Информация о Станции {self.station.id}")
        self.setMinimumWidth(500)

        self.layout = QVBoxLayout(self)

        # Основная информация о станции
        info_group = QGroupBox("Состояние станции")
        info_layout = QFormLayout()
        info_layout.addRow("Состояние:", QLabel(f"<b>{self.station.state.value}</b>"))
        info_layout.addRow("ID:", QLabel(str(self.station.id)))
        info_layout.addRow("NAV:", QLabel(str(self.station.nav)))
        info_layout.addRow("Backoff таймер:", QLabel(str(self.station.backoff_timer)))
        info_layout.addRow("Попыток:", QLabel(str(self.station.retry_counter)))
        info_layout.addRow("Ошибка:", QLabel("Да" if self.station.has_error else "Нет"))
        info_group.setLayout(info_layout)
        self.layout.addWidget(info_group)

        # Очередь сообщений
        self.layout.addWidget(QLabel("<hr><b>Очередь сообщений:</b>"))
        self.message_list = QListWidget()
        self.populate_messages()
        self.layout.addWidget(self.message_list)

        # История передач
        if station.transmission_history:
            self.layout.addWidget(QLabel("<hr><b>История передач:</b>"))
            history_list = QListWidget()
            for record in station.transmission_history[-10:]:  # Последние 10 записей
                status = "✓" if record['success'] else "✗"
                history_list.addItem(f"{status} {record['type'].value}")
            self.layout.addWidget(history_list)

        self.close_button = QPushButton("Закрыть")
        self.close_button.clicked.connect(self.accept)
        self.layout.addWidget(self.close_button)

    def populate_messages(self):
        if not self.station.message_queue:
            self.message_list.addItem("Очередь пуста")
        else:
            for msg in self.station.message_queue:
                item_text = f"Сообщение #{msg.message_id} для ст. {msg.receiver_id}: '{msg.data}'"
                self.message_list.addItem(item_text)


class StationGraphicsItem(QGraphicsEllipseItem):
    """Визуальное представление станции на сцене."""

    def __init__(self, station: Station, main_window):
        super().__init__(0, 0, 60, 60)  # Увеличен размер для лучшей видимости
        self.station = station
        self.main_window = main_window
        self.setPos(station.x, station.y)
        self.setBrush(QBrush(STATE_COLORS[station.state]))
        self.setPen(QPen(Qt.GlobalColor.black, 2))

        self.setFlag(QGraphicsEllipseItem.GraphicsItemFlag.ItemIsMovable)
        self.setFlag(QGraphicsEllipseItem.GraphicsItemFlag.ItemSendsGeometryChanges)

        # ID станции
        self.id_text = QGraphicsTextItem(str(station.id), self)
        self.id_text.setDefaultTextColor(QColor("black"))
        font = QFont()
        font.setBold(True)
        font.setPointSize(12)
        self.id_text.setFont(font)
        self.id_text.setPos(22, 18)

        # Состояние станции
        self.state_text = QGraphicsTextItem("", self)
        self.state_text.setDefaultTextColor(QColor("darkblue"))
        font = QFont()
        font.setPointSize(8)
        self.state_text.setFont(font)
        self.state_text.setPos(5, 62)

        # Индикатор ошибки
        self.error_indicator = QGraphicsEllipseItem(50, 5, 10, 10, self)
        self.error_indicator.setBrush(QBrush(QColor("purple")))
        self.error_indicator.setPen(QPen(Qt.GlobalColor.transparent))
        self.error_indicator.setVisible(station.has_error)

    def update_state(self):
        # Обновление цвета
        self.setBrush(QBrush(STATE_COLORS[self.station.state]))

        # Обновление рамки для ошибок
        pen = QPen(Qt.GlobalColor.black, 2)
        if self.station.has_error:
            pen = QPen(QColor("purple"), 3, Qt.PenStyle.DashDotLine)
        self.setPen(pen)

        # Показываем/скрываем индикатор ошибки
        self.error_indicator.setVisible(self.station.has_error)

        # Обновление текста состояния
        state_info = ""
        if self.station.state == StationState.BACKOFF:
            state_info = f"Backoff: {self.station.backoff_timer}"
        elif self.station.nav > 0:
            state_info = f"NAV: {self.station.nav}"
        elif self.station.state == StationState.WAITING_CTS:
            state_info = f"Ожидание CTS..."
        elif self.station.state == StationState.WAITING_ACK:
            state_info = f"Ожидание ACK..."

        self.state_text.setPlainText(state_info)

    def itemChange(self, change, value):
        if change == QGraphicsEllipseItem.GraphicsItemChange.ItemPositionChange and self.scene():
            self.station.x = value.x()
            self.station.y = value.y()
            self.main_window.update_communication_link()
        return super().itemChange(change, value)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        new_pos = self.pos()
        self.station.x = new_pos.x()
        self.station.y = new_pos.y()
        self.main_window.log_output.append(
            f"[Станция {self.station.id}] перемещена в ({int(self.station.x)}, {int(self.station.y)})"
        )

    def mousePressEvent(self, event):
        """Обрабатывает нажатия мыши"""
        if event.button() == Qt.MouseButton.RightButton:
            self.show_details_dialog()
            event.accept()
        else:
            super().mousePressEvent(event)

    def show_details_dialog(self):
        was_running = self.main_window.timer.isActive()
        if was_running:
            self.main_window.stop_simulation()

        dialog = MessageQueueDialog(self.station, self.main_window)
        dialog.exec()


class PacketAnimation(QGraphicsLineItem):
    """Анимация пакета (движущаяся точка по линии)"""

    def __init__(self, start_point, end_point, packet_type):
        super().__init__(start_point.x(), start_point.y(), end_point.x(), end_point.y())
        self.packet_type = packet_type
        self.animation_progress = 0
        self.animation_speed = 0.05

        style = PACKET_LINE_STYLES.get(packet_type)
        if style:
            pen = QPen(style["color"], style["width"])
            pen.setStyle(style["style"])
            self.setPen(pen)

        # Точка пакета
        self.packet_dot = QGraphicsEllipseItem(-5, -5, 10, 10, self)
        self.packet_dot.setBrush(QBrush(style["color"] if style else QColor("black")))

        # Текст типа пакета
        self.packet_text = QGraphicsTextItem(packet_type.value, self)
        self.packet_text.setDefaultTextColor(QColor("white"))
        font = QFont()
        font.setBold(True)
        font.setPointSize(8)
        self.packet_text.setFont(font)

    def update_animation(self):
        self.animation_progress += self.animation_speed
        if self.animation_progress > 1:
            self.animation_progress = 0

        # Вычисляем позицию пакета на линии
        line = self.line()
        dx = line.x2() - line.x1()
        dy = line.y2() - line.y1()

        x = line.x1() + dx * self.animation_progress
        y = line.y1() + dy * self.animation_progress

        self.packet_dot.setPos(x, y)
        self.packet_text.setPos(x - 10, y - 20)


class MainWindow(QMainWindow):
    """Главное окно приложения."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Симулятор протокола CSMA/CA с RTS/CTS")
        self.setGeometry(100, 100, 1400, 900)

        self.protocol = CSMACAProtocol()
        self.station_items: Dict[int, StationGraphicsItem] = {}
        self.message_counter = 1
        self.collision_indicator: Optional[QGraphicsSimpleTextItem] = None
        self.packet_animations: List[PacketAnimation] = []
        self.channel_status_widget = None

        self.timer = QTimer(self)
        self.timer.setInterval(200)  # Увеличен интервал для лучшей визуализации
        self.timer.timeout.connect(self.update_simulation)

        self.setup_ui()
        self.init_simulation()

    def setup_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QHBoxLayout(main_widget)

        # Левая панель - визуализация
        left_panel = QVBoxLayout()

        # Статусная панель
        status_layout = QHBoxLayout()
        self.step_label = QLabel("Шаг: 0")
        self.collision_label = QLabel("Коллизии: 0")
        self.success_label = QLabel("Успешные передачи: 0")
        self.channel_label = QLabel("Канал: Свободен")

        status_layout.addWidget(self.step_label)
        status_layout.addWidget(self.collision_label)
        status_layout.addWidget(self.success_label)
        status_layout.addWidget(self.channel_label)
        status_layout.addStretch()

        left_panel.addLayout(status_layout)

        # Графическая сцена
        self.scene = QGraphicsScene()
        self.scene.setSceneRect(0, 0, 900, 700)
        self.view = QGraphicsView(self.scene)
        self.view.setRenderHint(QPainter.RenderHint.Antialiasing)
        left_panel.addWidget(self.view)

        # Индикатор связи
        self.communication_link_item = QGraphicsLineItem()
        self.communication_link_item.setZValue(-1)
        self.scene.addItem(self.communication_link_item)
        self.communication_link_item.hide()

        main_layout.addLayout(left_panel, 2)

        # Правая панель - управление
        right_panel = QVBoxLayout()

        # Вкладки для лучшей организации
        self.tab_widget = QTabWidget()

        # Вкладка управления симуляцией
        controls_tab = QWidget()
        controls_layout = QVBoxLayout()

        controls_group = QGroupBox("Управление симуляцией")
        controls_inner = QVBoxLayout()

        # Скорость симуляции
        speed_layout = QHBoxLayout()
        speed_layout.addWidget(QLabel("Скорость:"))
        self.speed_slider = QSpinBox()
        self.speed_slider.setRange(1, 10)
        self.speed_slider.setValue(5)
        self.speed_slider.valueChanged.connect(self.update_simulation_speed)
        speed_layout.addWidget(self.speed_slider)
        controls_inner.addLayout(speed_layout)

        # Кнопки управления
        self.start_button = QPushButton("▶ Старт")
        self.start_button.clicked.connect(self.start_simulation)
        controls_inner.addWidget(self.start_button)

        self.stop_button = QPushButton("⏹ Стоп")
        self.stop_button.clicked.connect(self.stop_simulation)
        self.stop_button.setEnabled(False)
        controls_inner.addWidget(self.stop_button)

        self.step_button = QPushButton("⏯ Шаг")
        self.step_button.clicked.connect(self.step_simulation)
        controls_inner.addWidget(self.step_button)

        self.reset_button = QPushButton("🔄 Сброс")
        self.reset_button.clicked.connect(self.reset_simulation)
        controls_inner.addWidget(self.reset_button)

        controls_group.setLayout(controls_inner)
        controls_layout.addWidget(controls_group)

        # Статистика
        stats_group = QGroupBox("Статистика сети")
        stats_layout = QVBoxLayout()
        self.stats_text = QTextEdit()
        self.stats_text.setReadOnly(True)
        self.stats_text.setMaximumHeight(150)
        stats_layout.addWidget(self.stats_text)
        controls_group.setLayout(stats_layout)
        controls_layout.addWidget(controls_group)

        controls_layout.addStretch()
        controls_tab.setLayout(controls_layout)
        self.tab_widget.addTab(controls_tab, "Управление")

        # Вкладка станций
        stations_tab = QWidget()
        stations_layout = QVBoxLayout()

        station_group = QGroupBox("Добавить станцию")
        station_layout = QFormLayout()
        self.station_x = QLineEdit("100")
        self.station_y = QLineEdit("100")
        self.add_station_button = QPushButton("➕ Добавить")
        self.add_station_button.clicked.connect(self.add_station)
        station_layout.addRow("X:", self.station_x)
        station_layout.addRow("Y:", self.station_y)
        station_layout.addWidget(self.add_station_button)
        station_group.setLayout(station_layout)
        stations_layout.addWidget(station_group)

        delete_group = QGroupBox("Удалить станцию")
        delete_layout = QFormLayout()
        self.delete_station_id_combo = QComboBox()
        self.delete_station_button = QPushButton("🗑 Удалить")
        self.delete_station_button.clicked.connect(self.delete_station)
        delete_layout.addRow("ID Станции:", self.delete_station_id_combo)
        delete_layout.addWidget(self.delete_station_button)
        delete_group.setLayout(delete_layout)
        stations_layout.addWidget(delete_group)

        stations_layout.addStretch()
        stations_tab.setLayout(stations_layout)
        self.tab_widget.addTab(stations_tab, "Станции")

        # Вкладка сообщений
        messages_tab = QWidget()
        messages_layout = QVBoxLayout()

        message_group = QGroupBox("Отправить сообщение")
        message_layout = QFormLayout()
        self.sender_id_combo = QComboBox()
        self.receiver_id_combo = QComboBox()
        self.message_data_input = QLineEdit("Привет!")
        self.send_message_button = QPushButton("📤 Отправить")
        self.send_message_button.clicked.connect(self.send_message)
        message_layout.addRow("Отправитель:", self.sender_id_combo)
        message_layout.addRow("Получатель:", self.receiver_id_combo)
        message_layout.addRow("Данные:", self.message_data_input)
        message_layout.addWidget(self.send_message_button)
        message_group.setLayout(message_layout)
        messages_layout.addWidget(message_group)

        messages_layout.addStretch()
        messages_tab.setLayout(messages_layout)
        self.tab_widget.addTab(messages_tab, "Сообщения")

        # Вкладка ошибок
        errors_tab = QWidget()
        errors_layout = QVBoxLayout()

        error_group = QGroupBox("Управление ошибками")
        error_layout = QFormLayout()
        self.error_station_id_combo = QComboBox()
        self.inject_error_button = QPushButton("⚠ Внести ошибку")
        self.inject_error_button.clicked.connect(self.inject_error)
        self.fix_error_button = QPushButton("✅ Устранить ошибку")
        self.fix_error_button.clicked.connect(self.fix_error)
        error_layout.addRow("ID Станции:", self.error_station_id_combo)
        error_layout.addWidget(self.inject_error_button)
        error_layout.addWidget(self.fix_error_button)
        error_group.setLayout(error_layout)
        errors_layout.addWidget(error_group)

        errors_layout.addStretch()
        errors_tab.setLayout(errors_layout)
        self.tab_widget.addTab(errors_tab, "Ошибки")

        right_panel.addWidget(self.tab_widget)

        # Настройки лога
        self.autoscroll_checkbox = QCheckBox("Автопрокрутка лога")
        self.autoscroll_checkbox.setChecked(True)
        right_panel.addWidget(self.autoscroll_checkbox)

        # Лог
        log_group = QGroupBox("Лог транзакций")
        log_layout = QVBoxLayout()
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setMaximumHeight(250)
        log_layout.addWidget(self.log_output)
        log_group.setLayout(log_layout)
        right_panel.addWidget(log_group)

        # Легенда цветов
        legend_group = QGroupBox("Легенда состояний")
        legend_layout = QVBoxLayout()
        legend_text = """
        <b>Цвета станций:</b><br>
        <span style="color:lightblue">█</span> Ожидание<br>
        <span style="color:lightyellow">█</span> Прослушивание<br>
        <span style="color:orange">█</span> Отправка RTS<br>
        <span style="color:yellow">█</span> Ожидание CTS<br>
        <span style="color:red">█</span> Передача данных<br>
        <span style="color:pink">█</span> Ожидание ACK<br>
        <span style="color:lightgreen">█</span> Прием данных<br>
        <span style="color:lightgray">█</span> Backoff<br>
        <span style="color:darkred">█</span> Ошибка<br>
        <span style="color:purple">●</span> Станция с ошибкой<br>
        <hr>
        <b>Типы пакетов:</b><br>
        <span style="color:orange">━━━</span> RTS<br>
        <span style="color:gold">━━━</span> CTS<br>
        <span style="color:red">━━━</span> DATA<br>
        <span style="color:green">····</span> ACK
        """
        legend_label = QLabel(legend_text)
        legend_label.setWordWrap(True)
        legend_layout.addWidget(legend_label)
        legend_group.setLayout(legend_layout)
        right_panel.addWidget(legend_group)

        main_layout.addLayout(right_panel, 1)

    def init_simulation(self):
        self.protocol = CSMACAProtocol()
        self.scene.clear()

        # Добавляем индикатор канала
        self.channel_status_widget = ChannelStatusWidget(400, 50, self.protocol)
        self.scene.addItem(self.channel_status_widget)

        self.communication_link_item = QGraphicsLineItem()
        self.communication_link_item.setZValue(-1)
        self.scene.addItem(self.communication_link_item)
        self.communication_link_item.hide()

        self.collision_indicator = None
        self.station_items.clear()
        self.packet_animations.clear()
        self.log_output.clear()
        self.message_counter = 1

        # Добавляем начальные станции
        self.add_station(is_initial=True, x=200, y=300)
        self.add_station(is_initial=True, x=600, y=300)
        self.add_station(is_initial=True, x=400, y=500)

        self.update_station_id_selectors()
        self.update_communication_link()
        self.update_statistics()

    def update_simulation_speed(self):
        speed = self.speed_slider.value()
        interval = 300 - (speed * 25)  # От 275 до 50 мс
        self.timer.setInterval(interval)

    def add_station(self, is_initial=False, x=None, y=None):
        try:
            pos_x = float(self.station_x.text()) if x is None else x
            pos_y = float(self.station_y.text()) if y is None else y

            station = self.protocol.add_station(pos_x, pos_y)
            item = StationGraphicsItem(station, self)
            self.station_items[station.id] = item
            self.scene.addItem(item)

            if not is_initial:
                self.log_output.append(f"✅ Добавлена станция {station.id} в ({pos_x}, {pos_y})")
            self.update_station_id_selectors()
        except ValueError:
            self.log_output.append("❌ Ошибка: Неверные координаты для станции.")

    def delete_station(self):
        if not self.delete_station_id_combo.currentText():
            self.log_output.append("❌ Нет станций для удаления.")
            return
        station_id = int(self.delete_station_id_combo.currentText())
        if station_id in self.station_items:
            item_to_remove = self.station_items[station_id]
            self.scene.removeItem(item_to_remove)
            del self.station_items[station_id]
            self.protocol.remove_station(station_id)
            self.log_output.append(f"🗑 Станция {station_id} удалена.")
            self.update_station_id_selectors()
            self.update_communication_link()
        else:
            self.log_output.append(f"❌ Ошибка: Станция {station_id} не найдена.")

    def update_station_id_selectors(self):
        ids = sorted([str(s.id) for s in self.protocol.stations], key=int)

        current_sender = self.sender_id_combo.currentText()
        current_receiver = self.receiver_id_combo.currentText()
        current_delete = self.delete_station_id_combo.currentText()
        current_error = self.error_station_id_combo.currentText()

        combos = [self.sender_id_combo, self.receiver_id_combo,
                  self.delete_station_id_combo, self.error_station_id_combo]
        for combo in combos:
            combo.clear()
            combo.addItems(ids)

        if current_sender in ids: self.sender_id_combo.setCurrentText(current_sender)
        if current_receiver in ids: self.receiver_id_combo.setCurrentText(current_receiver)
        if current_delete in ids: self.delete_station_id_combo.setCurrentText(current_delete)
        if current_error in ids: self.error_station_id_combo.setCurrentText(current_error)

    def inject_error(self):
        station_id_str = self.error_station_id_combo.currentText()
        if not station_id_str:
            self.log_output.append("❌ Ошибка: Станция для внесения ошибки не выбрана.")
            return
        station = self.protocol.get_station(int(station_id_str))
        if station:
            station.set_error(True)
            self.log_output.append(f"⚠ Внесена ошибка в станцию {station.id}.")
            self.station_items[station.id].update_state()
        else:
            self.log_output.append(f"❌ Ошибка: Не удалось найти станцию {station_id_str}.")

    def fix_error(self):
        station_id_str = self.error_station_id_combo.currentText()
        if not station_id_str:
            self.log_output.append("❌ Ошибка: Станция для устранения ошибки не выбрана.")
            return
        station = self.protocol.get_station(int(station_id_str))
        if station:
            station.set_error(False)
            self.log_output.append(f"✅ Ошибка на станции {station.id} устранена.")
            self.station_items[station.id].update_state()
        else:
            self.log_output.append(f"❌ Ошибка: Не удалось найти станцию {station_id_str}.")

    def send_message(self):
        sender_id_str = self.sender_id_combo.currentText()
        receiver_id_str = self.receiver_id_combo.currentText()
        if not sender_id_str or not receiver_id_str:
            self.log_output.append("❌ Ошибка: Необходимо выбрать отправителя и получателя.")
            return
        sender_id = int(sender_id_str)
        receiver_id = int(receiver_id_str)
        data = self.message_data_input.text()
        if sender_id == receiver_id:
            self.log_output.append("❌ Ошибка: Отправитель и получатель не могут совпадать.")
            return
        sender = self.protocol.get_station(sender_id)
        if sender and data:
            sender.add_message(receiver_id, data, self.message_counter)
            self.log_output.append(
                f"📨 [Сообщение #{self.message_counter}] Станция {sender_id} -> Станция {receiver_id}: '{data}' добавлено в очередь.")
            self.message_counter += 1
        else:
            self.log_output.append("❌ Ошибка: Не удалось добавить сообщение.")

    def start_simulation(self):
        self.timer.start()
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.step_button.setEnabled(False)

    def stop_simulation(self):
        self.timer.stop()
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.step_button.setEnabled(True)

    def step_simulation(self):
        self.update_simulation()

    def reset_simulation(self):
        self.stop_simulation()
        self.init_simulation()
        self.log_output.append("🔄 Симуляция сброшена.")

    def update_simulation(self):
        logs = self.protocol.process_step()
        if logs:
            self.log_output.append(f"--- Шаг {self.protocol.step_counter} ---")
            for log_entry in logs:
                self.log_output.append(log_entry)

        self.update_station_visuals()
        self.update_communication_link()
        self.handle_collision_visuals()
        self.update_statistics()
        self.update_packet_animations()

        if self.channel_status_widget:
            self.channel_status_widget.update_status()

        if self.autoscroll_checkbox.isChecked():
            self.log_output.verticalScrollBar().setValue(self.log_output.verticalScrollBar().maximum())

    def update_packet_animations(self):
        # Очищаем старые анимации
        for anim in self.packet_animations:
            self.scene.removeItem(anim)
        self.packet_animations.clear()

        # Создаем новую анимацию если есть передача
        transmission = self.protocol.current_transmission
        if transmission and transmission.sender_id in self.station_items and transmission.receiver_id in self.station_items:
            sender_item = self.station_items[transmission.sender_id]
            receiver_item = self.station_items[transmission.receiver_id]

            center_offset = sender_item.rect().width() / 2
            start_point = sender_item.pos() + QPointF(center_offset, center_offset)
            end_point = receiver_item.pos() + QPointF(center_offset, center_offset)

            animation = PacketAnimation(start_point, end_point, transmission.packet_type)
            self.packet_animations.append(animation)
            self.scene.addItem(animation)

    def update_station_visuals(self):
        for station_id, item in self.station_items.items():
            station = self.protocol.get_station(station_id)
            if station:
                item.station = station
                item.update_state()

    def handle_collision_visuals(self):
        collided_stations = self.protocol.last_collision_stations
        if not collided_stations:
            if self.collision_indicator:
                self.scene.removeItem(self.collision_indicator)
                self.collision_indicator = None
            return

        avg_x, avg_y = 0, 0
        station_count = 0
        for station in collided_stations:
            if station.id in self.station_items:
                item = self.station_items[station.id]
                pos = item.pos()
                avg_x += pos.x()
                avg_y += pos.y()
                station_count += 1
            else:
                continue

        if station_count == 0:
            return

        center_offset = 30  # Половина размера станции
        center_point = QPointF((avg_x / station_count) + center_offset, (avg_y / station_count) + center_offset)

        if not self.collision_indicator:
            self.collision_indicator = QGraphicsSimpleTextItem("💥 КОЛЛИЗИЯ!")
            font = QFont()
            font.setPointSize(20)
            font.setBold(True)
            self.collision_indicator.setFont(font)
            self.collision_indicator.setBrush(QBrush(QColor("red")))

        self.collision_indicator.setPos(center_point)
        self.collision_indicator.setZValue(10)
        if self.collision_indicator not in self.scene.items():
            self.scene.addItem(self.collision_indicator)

        # Мигающий эффект
        current_time = time.time()
        if int(current_time * 2) % 2 == 0:
            self.collision_indicator.setVisible(True)
        else:
            self.collision_indicator.setVisible(False)

    def update_statistics(self):
        stats = self.protocol.get_statistics()
        stats_text = f"""
        <b>Статистика сети:</b><br>
        Станций: {stats['total_stations']}<br>
        Шаг симуляции: {stats['total_steps']}<br>
        Успешные передачи: {stats['successful_transmissions']}<br>
        Неудачные передачи: {stats['failed_transmissions']}<br>
        Коллизии: {stats['total_collisions']}<br>
        Использование канала: {stats['channel_utilization']}<br>
        Сообщений в очередях: {stats['total_messages']}
        """
        self.stats_text.setHtml(stats_text)

        # Обновляем статусные метки
        self.step_label.setText(f"Шаг: {stats['total_steps']}")
        self.collision_label.setText(f"Коллизии: {stats['total_collisions']}")
        self.success_label.setText(f"Успешные: {stats['successful_transmissions']}")
        self.channel_label.setText(f"Канал: {'Занят' if self.protocol.channel_busy else 'Свободен'}")

    def update_communication_link(self):
        transmission = self.protocol.current_transmission
        if not transmission:
            self.communication_link_item.hide()
            return

        sender_id = transmission.sender_id
        receiver_id = transmission.receiver_id

        if sender_id in self.station_items and receiver_id in self.station_items:
            sender_item = self.station_items[sender_id]
            receiver_item = self.station_items[receiver_id]

            center_offset = sender_item.rect().width() / 2
            p1 = sender_item.pos()
            p2 = receiver_item.pos()

            self.communication_link_item.setLine(
                p1.x() + center_offset, p1.y() + center_offset,
                p2.x() + center_offset, p2.y() + center_offset
            )

            pen = QPen()
            style_info = PACKET_LINE_STYLES.get(transmission.packet_type)
            if style_info:
                pen.setColor(style_info["color"])
                pen.setStyle(style_info["style"])
                pen.setWidth(style_info["width"])
            else:
                pen.setColor(QColor("black"))
                pen.setWidth(2)

            self.communication_link_item.setPen(pen)
            self.communication_link_item.show()
        else:
            self.communication_link_item.hide()


# ==================== Main Entry Point ====================

def main():
    random.seed(time.time())
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()