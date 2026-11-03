# Synthesis, placement and routing of itch_top, out of context (the block design adds the pins).
# Non-project mode. Target: the ZCU102.
#
#     vivado -mode batch -source scripts/synth.tcl -log build/vivado.log -journal build/vivado.jou
#
# Reports go to build/reports. Change PART for another device (the speed grade too). itch_top's
# TABLE_STYLE default is "block" (the ZU9EG has no URAM); for a device with URAM change the
# default to "ultra" (passing string generics on the command line was not reliable in 2019.2).

set PART xczu9eg-ffvb1156-2-e
set TOP  itch_top

set root    [file normalize [file dirname [info script]]/..]
set build   $root/build
set reports $build/reports
file mkdir $reports

set sources {
    itch_pkg itch_hash_pkg ram_sdp lutram counters packet_parser framer h3_hash decoder
    price_unit ot_bank ot_stash order_table bitmap_search ladder_side book_engine event_delay
    output_stage itch_core regs_axil xgmii_rx record_fifo record_packer xgmii_tx record_tx itch_top
}
foreach name $sources {
    read_vhdl -vhdl2008 $root/rtl/$name.vhd
}
read_xdc -mode out_of_context $root/constraints/clock.xdc
read_xdc -mode out_of_context $root/constraints/record_cdc.xdc

synth_design -top $TOP -part $PART -mode out_of_context
write_checkpoint -force $build/synth.dcp
report_utilization            -file $reports/synth_utilization.txt
report_utilization -hierarchical -hierarchical_depth 3 -file $reports/synth_utilization_hierarchy.txt
report_ram_utilization        -file $reports/synth_ram.txt
report_timing_summary         -file $reports/synth_timing.txt
report_timing -sort_by group -max_paths 100 -nworst 1 -file $reports/synth_worst_paths.txt

opt_design
place_design
phys_opt_design
route_design
write_checkpoint -force $build/routed.dcp

report_utilization            -file $reports/utilization.txt
report_utilization -hierarchical -hierarchical_depth 3 -file $reports/utilization_hierarchy.txt
report_ram_utilization        -file $reports/ram.txt
report_timing_summary -max_paths 20 -report_unconstrained -file $reports/timing_summary.txt
report_timing -sort_by group -max_paths 50 -nworst 1 -file $reports/worst_paths.txt
report_design_analysis -logic_level_distribution -file $reports/logic_levels.txt
report_methodology            -file $reports/methodology.txt

# latches would mean an incomplete combinational process
set latches [get_cells -hierarchical -quiet -filter {PRIMITIVE_TYPE =~ REGISTER.LATCH.*}]
puts "LATCHES: [llength $latches]"
puts "WNS: [get_property SLACK [get_timing_paths -max_paths 1 -setup]] ns"
