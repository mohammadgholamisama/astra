package models

import "time"

// Operator is the Iranian mobile network used for real-delay samples.
type Operator string

const (
	OperatorIrancell Operator = "irancell"
	OperatorMCI      Operator = "mci"
	OperatorRightel  Operator = "rightel"
)

var AllOperators = []Operator{
	OperatorIrancell,
	OperatorMCI,
	OperatorRightel,
}

func (o Operator) Valid() bool {
	switch o {
	case OperatorIrancell, OperatorMCI, OperatorRightel:
		return true
	default:
		return false
	}
}

func (o Operator) LabelFA() string {
	switch o {
	case OperatorIrancell:
		return "ایرانسل"
	case OperatorMCI:
		return "همراه اول"
	case OperatorRightel:
		return "رایتل"
	default:
		return string(o)
	}
}

// PingSample is one real-delay (+ optional speed) measurement from a phone.
type PingSample struct {
	ConfigHash   string   `json:"config_hash"`
	Operator     Operator `json:"operator"`
	PingMs       int64    `json:"ping_ms"` // -1 = timeout / unreachable
	DownloadKbps float64  `json:"download_kbps,omitempty"`
	UploadKbps   float64  `json:"upload_kbps,omitempty"`
	Remark       string   `json:"remark,omitempty"`
	Protocol     string   `json:"protocol,omitempty"`
	Transport    string   `json:"transport,omitempty"`
	Security     string   `json:"security,omitempty"`
	Host         string   `json:"host,omitempty"`
	Port         int      `json:"port,omitempty"`
	MatchHost    string   `json:"match_host,omitempty"` // SNI / peer used by Astra order API
	DeviceID     string   `json:"device_id,omitempty"`
	MeasuredAt   int64    `json:"measured_at,omitempty"` // unix ms; server fills if 0
}

// PingBatchRequest is posted by the mobile app after a test cycle.
type PingBatchRequest struct {
	Operator Operator     `json:"operator"`
	DeviceID string       `json:"device_id,omitempty"`
	Samples  []PingSample `json:"samples"`
}

// OperatorPing holds the latest ping for one operator (or -1 / missing).
type OperatorPing struct {
	Operator   Operator `json:"operator"`
	LabelFA    string   `json:"label_fa"`
	PingMs     int64    `json:"ping_ms"`
	UpdatedAt  int64    `json:"updated_at"`
	HasSample  bool     `json:"has_sample"`
}

// RankedConfig is the sorted leaderboard row pushed over HTTP + WebSocket.
type RankedConfig struct {
	Rank          int            `json:"rank"`
	ConfigHash    string         `json:"config_hash"`
	Remark        string         `json:"remark"`
	Protocol      string         `json:"protocol"`
	Transport     string         `json:"transport"`
	Security      string         `json:"security"`
	Host          string         `json:"host"`
	Port          int            `json:"port"`
	MatchHost     string         `json:"match_host"`
	SuccessRate   float64        `json:"success_rate"`
	AvgPing       float64        `json:"avg_ping"`
	OperatorPings []OperatorPing `json:"operator_pings"`
	UpdatedAt     time.Time      `json:"updated_at"`
	DeviceCount   int            `json:"device_count,omitempty"`
}

// FingerprintRank is host:port[+match_host] averaged across devices — Astra order key.
type FingerprintRank struct {
	Host            string  `json:"host"`
	Port            int     `json:"port"`
	MatchHost       string  `json:"match_host"`
	Label           string  `json:"label,omitempty"`
	AvgPing         float64 `json:"avg_ping"`
	DownloadKbps    float64 `json:"download_kbps,omitempty"`
	UploadKbps      float64 `json:"upload_kbps,omitempty"`
	SampleCount     int     `json:"sample_count"`
	DeviceCount     int     `json:"device_count"`
	FailDeviceCount int     `json:"fail_device_count,omitempty"`
}

// PingRankBody is POSTed to Astra /api/admin/config-order/ping-rank.
type PingRankBody struct {
	Items []FingerprintRank `json:"items"`
}

// RankedListResponse is returned by GET /api/v1/ranked.
type RankedListResponse struct {
	GeneratedAt time.Time      `json:"generated_at"`
	Count       int            `json:"count"`
	Configs     []RankedConfig `json:"configs"`
}

// PanelOrderItem matches Astra PUT /api/admin/config-order body items.
type PanelOrderItem struct {
	Host      string  `json:"host"`
	Port      int     `json:"port"`
	MatchHost string  `json:"match_host"`
	Label     *string `json:"label"`
}

// PanelOrderBody is the Astra config-order save payload.
type PanelOrderBody struct {
	Items []PanelOrderItem `json:"items"`
}

// HealthResponse is a lightweight liveness payload.
type HealthResponse struct {
	OK      bool   `json:"ok"`
	Service string `json:"service"`
	Time    int64  `json:"time"`
}

// WSMessage wraps live ranking broadcasts.
type WSMessage struct {
	Type    string             `json:"type"`
	Payload RankedListResponse `json:"payload"`
}
