# Proposed addition to initramfs/i14charge (not installed): charge_wait through the Rust
# i14-charge-wait, without eval. Sets why, cap, vol and returns 0 (start Linux), 2 (power
# off) or 1 (rescue). Only exit 0/1/2 with exactly four lines - why=, cap=, vol=, end - is
# used; anything else is rescue: a missing, crashing or interrupted binary, or any other
# output, can never start Linux.
charge_wait_rs() {
	cw_out=$(AB_PCT=$AB_PCT AB_MIN_S=$AB_MIN_S AB_HOLD_S=$AB_HOLD_S AB_MAX_S=$AB_MAX_S i14-charge-wait)
	cw_rc=$?
	cw_nl='
'
	cw_cr=$(printf '\r')
	case $cw_rc in 0|1|2) ;; *) cw_out= ;; esac
	case $cw_out in *"$cw_cr"*) cw_out= ;; esac  # i14-charge-wait never prints a CR
	cw_why=${cw_out#why=}; cw_why=${cw_why%%"$cw_nl"*}
	cw_cap=${cw_out#*"${cw_nl}cap="}; cw_cap=${cw_cap%%"$cw_nl"*}
	cw_vol=${cw_out#*"${cw_nl}vol="}; cw_vol=${cw_vol%%"$cw_nl"*}
	# The whole output must be exactly the frame rebuilt from the three values.
	if [ -z "$cw_out" ] || [ "$cw_out" != "why=$cw_why${cw_nl}cap=$cw_cap${cw_nl}vol=$cw_vol${cw_nl}end" ]; then
		why="i14-charge-wait failed (exit $cw_rc)"; cap=; vol=
		return 1
	fi
	why=$cw_why cap=$cw_cap vol=$cw_vol
	return $cw_rc
}
