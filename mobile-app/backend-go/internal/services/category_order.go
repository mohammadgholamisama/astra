package services

import (
	"fmt"
	"regexp"
	"sort"
	"strings"

	"github.com/mohammadgholamisama/v2ray-ping-hub/internal/models"
)

const categorySlot = 5

var (
	reGaming = regexp.MustCompile(`(?i)گیمینگ|gaming|\bgame\b|گیم\b`)
	reTunnel = regexp.MustCompile(`(?i)تانل|تونل|tunnel`)
	reDirect = regexp.MustCompile(`(?i)مستقیم|mostaghim|direct`)
)

func detectCategory(label string) string {
	t := strings.ToLower(label)
	if reGaming.MatchString(t) {
		return "gaming"
	}
	if reTunnel.MatchString(t) {
		return "tunnel"
	}
	if reDirect.MatchString(t) {
		return "direct"
	}
	return "other"
}

// arrangeFingerprintSlots: 5 مستقیم → 5 تانل → 5 گیمینگ → rest by quality.
// Failures (avg<=0) stay after successful items.
func arrangeFingerprintSlots(items []models.FingerprintRank) []models.FingerprintRank {
	ok := make([]models.FingerprintRank, 0, len(items))
	rest := make([]models.FingerprintRank, 0)
	for _, it := range items {
		if it.AvgPing > 0 {
			ok = append(ok, it)
		} else {
			rest = append(rest, it)
		}
	}

	quality := func(a, b models.FingerprintRank) bool {
		da, db := a.DeviceCount, b.DeviceCount
		if da < 1 {
			da = 1
		}
		if db < 1 {
			db = 1
		}
		if da != db {
			return da > db
		}
		if a.AvgPing != b.AvgPing {
			return a.AvgPing < b.AvgPing
		}
		if a.Host != b.Host {
			return a.Host < b.Host
		}
		return a.Port < b.Port
	}

	buckets := map[string][]models.FingerprintRank{
		"direct": {},
		"tunnel": {},
		"gaming": {},
		"other":  {},
	}
	for _, it := range ok {
		cat := detectCategory(it.Label)
		buckets[cat] = append(buckets[cat], it)
	}
	for cat := range buckets {
		sort.SliceStable(buckets[cat], func(i, j int) bool {
			return quality(buckets[cat][i], buckets[cat][j])
		})
	}

	picked := map[string]struct{}{}
	keyOf := func(r models.FingerprintRank) string {
		return fmt.Sprintf("%s|%d|%s", strings.ToLower(r.Host), r.Port, strings.ToLower(r.MatchHost))
	}
	head := make([]models.FingerprintRank, 0, categorySlot*3)
	for _, cat := range []string{"direct", "tunnel", "gaming"} {
		list := buckets[cat]
		n := categorySlot
		if len(list) < n {
			n = len(list)
		}
		for i := 0; i < n; i++ {
			head = append(head, list[i])
			picked[keyOf(list[i])] = struct{}{}
		}
	}

	leftover := make([]models.FingerprintRank, 0)
	for _, it := range ok {
		if _, seen := picked[keyOf(it)]; !seen {
			leftover = append(leftover, it)
		}
	}
	sort.SliceStable(leftover, func(i, j int) bool {
		return quality(leftover[i], leftover[j])
	})
	sort.SliceStable(rest, func(i, j int) bool {
		if rest[i].Host != rest[j].Host {
			return rest[i].Host < rest[j].Host
		}
		return rest[i].Port < rest[j].Port
	})

	out := make([]models.FingerprintRank, 0, len(items))
	out = append(out, head...)
	out = append(out, leftover...)
	out = append(out, rest...)
	return out
}
