/* SPDX-License-Identifier: GPL-2.0-only */
/* Fixed I14 AArch64 RAM handoff: no stack, EL transition, or device I/O. */
.section .text, "ax"
.global _start
_start:
    b .Lentry
    nop
    .quad 0
    .quad OUTER_EFFECTIVE_SIZE
    .quad 10
    .quad 0, 0, 0
    .ascii "ARM\x64"
    .long 0

.Lentry:
    mrs x4, CurrentEL
    cmp x4, #4
    b.eq .Lel1
    cmp x4, #8
    b.ne .Lpark
    mrs x4, sctlr_el2
    b .Lstate
.Lel1:
    mrs x4, sctlr_el1
.Lstate:
    tbnz x4, #0, .Lpark          /* Physical addressing requires MMU off. */
    tbnz x4, #25, .Lpark         /* Literal data and kernel are little endian. */
    msr daifset, #15
    dsb sy
    adr x4, .Ldtb
    mov x5, #0x4e000000
    ldr x6, =DTB_SIZE
.Lcopy:
    ldrb w7, [x4], #1
    strb w7, [x5], #1
    subs x6, x6, #1
    b.ne .Lcopy

    mrs x9, ctr_el0
    ubfx x9, x9, #16, #4
    mov x10, #4
    lsl x10, x10, x9
    dsb sy
    mov x4, #0x40000000
    ldr x5, =OUTER_FILE_END
    bl .Lclean
    mov x4, #0x4e000000
    ldr x5, =(0x4e000000 + DTB_SIZE)
    bl .Lclean
    dsb sy
    ic iallu
    dsb sy
    isb
    mov x0, #0x4e000000
    mov x1, xzr
    mov x2, xzr
    mov x3, xzr
    mov x4, #0x40200000
    br x4

.Lclean:
    dc civac, x4                /* Also covers the A53 CVAC erratum workaround. */
    add x4, x4, x10
    cmp x4, x5
    b.lo .Lclean
    ret
.Lpark:
    wfe
    b .Lpark
    .ltorg

.org 0x1000
.Ldtb:
    .incbin DTB_FILE
