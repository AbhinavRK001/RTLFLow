# RTLFlow OpenROAD prelude — helper procs used by the innovus shell & GUI.
# Loaded once into the persistent OpenROAD session. All procs are read-only
# queries except where noted; each returns a flat Tcl list "key value ...".

namespace eval rf {}

proc rf::block {} {
    set db [ord::get_db]
    if {$db eq "NULL" || $db eq ""} { return "" }
    set chip [$db getChip]
    if {$chip eq "NULL" || $chip eq ""} { return "" }
    set b [$chip getBlock]
    if {$b eq "NULL"} { return "" }
    return $b
}

proc rf::tech_loaded {} {
    set db [ord::get_db]
    set t [$db getTech]
    if {$t eq "NULL" || $t eq ""} { return 0 }
    return [expr {[llength [$t getLayers]] > 0}]
}

proc rf::lib_count {} {
    set n 0
    foreach lib [[ord::get_db] getLibs] { incr n [llength [$lib getMasters]] }
    return $n
}

# routing layers bottom→top: name dir pitch
proc rf::layers {} {
    set out {}
    set t [[ord::get_db] getTech]
    if {$t eq "NULL"} { return $out }
    foreach l [$t getLayers] {
        set type [$l getType]
        if {$type eq "ROUTING"} {
            set p 0
            catch {set p [$l getPitch]}
            lappend out [$l getName] [$l getDirection] $p
        }
    }
    return $out
}

proc rf::sites {} {
    set out {}
    foreach lib [[ord::get_db] getLibs] {
        foreach s [$lib getSites] {
            set cls "CORE"
            catch {set cls [$s getClass]}
            lappend out [$s getName] $cls [$s getWidth] [$s getHeight]
        }
    }
    return $out
}

proc rf::masters {pattern} {
    set out {}
    foreach lib [[ord::get_db] getLibs] {
        foreach m [$lib getMasters] {
            set n [$m getName]
            if {[regexp -- $pattern $n]} { lappend out $n [$m getWidth] }
        }
    }
    return $out
}

proc rf::design_info {} {
    set b [rf::block]
    if {$b eq ""} { return {loaded 0} }
    set dbu [$b getDefUnits]
    set die [$b getDieArea]
    set out [list loaded 1 name [$b getName] dbu $dbu]
    lappend out die_x0 [$die xMin] die_y0 [$die yMin] die_x1 [$die xMax] die_y1 [$die yMax]
    set core_ok 0
    if {![catch {set core [$b getCoreArea]}]} {
        catch {
            lappend out core_x0 [$core xMin] core_y0 [$core yMin] core_x1 [$core xMax] core_y1 [$core yMax]
            set core_ok 1
        }
    }
    set n 0; set placed 0; set std_area 0; set seq 0
    foreach inst [$b getInsts] {
        incr n
        set st [$inst getPlacementStatus]
        if {$st ne "NONE" && $st ne "UNPLACED"} { incr placed }
        set m [$inst getMaster]
        set fill 0; catch {set fill [$m isFiller]}
        if {!$fill} {
            set std_area [expr {$std_area + double([$m getWidth]) * [$m getHeight]}]
        }
        set isseq 0; catch {set isseq [$m isSequential]}
        if {$isseq} { incr seq }
    }
    lappend out insts $n placed $placed seq $seq std_area_dbu2 $std_area
    lappend out nets [llength [$b getNets]] terms [llength [$b getBTerms]] rows [llength [$b getRows]]
    return $out
}

# worst slack / tns in library time units; "NA" if no clocks
proc rf::timing_summary {} {
    set out {}
    if {[catch {llength [all_clocks]} nclk] || $nclk == 0} { return {clocks 0} }
    lappend out clocks $nclk
    foreach {k mode} {wns max whs min} {
        set v NA
        # proc form returns user units (ns); *_cmd form returns SI seconds
        if {[catch {set v [sta::worst_slack -$mode]}]} {
            if {![catch {set v [sta::worst_slack_cmd $mode]}]} {
                catch {set v [sta::time_sta_ui $v]}
            }
        }
        if {$v ne "NA"} { catch {set v [format %.4f $v]} }
        lappend out $k $v
    }
    set tns NA
    if {[catch {set tns [sta::total_negative_slack -max]}]} {
        if {![catch {set tns [sta::total_negative_slack_cmd max]}]} {
            catch {set tns [sta::time_sta_ui $tns]}
        }
    }
    if {$tns ne "NA"} { catch {set tns [format %.4f $tns]} }
    lappend out tns $tns
    set viol NA
    catch {set viol [sta::endpoint_violation_count max]}
    lappend out viol $viol
    return $out
}

proc rf::report_paths {n} {
    # OpenSTA renamed -group_count → -group_path_count; accept both
    if {[catch {report_checks -path_delay max -group_path_count $n -format full_clock_expanded \
            -fields {slew cap input_pins fanout} -digits 3} err]} {
        report_checks -path_delay max -group_count $n -format full_clock_expanded \
            -fields {slew cap input_pins fanout} -digits 3
    }
}
