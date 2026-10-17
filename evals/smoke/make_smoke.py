"""Build the synthetic smoke datasets in evals/datasets/.

These are hand-written, FFE-style functions whose subsystem is known because
we wrote them that way. They are NOT experiment-confirmed and never enter the
ledger. Their only job is to exercise the eval harness and the CI gate before
the emulator produces real settled labels (then `ffelab eval export` takes
over). Scores on them say little about how the agents do on the real game.

    python evals/smoke/make_smoke.py
"""
from __future__ import annotations

from pathlib import Path

from ffelab.evals.dataset import EvalCase, TargetClaim, pre_experiment_test, save, split_for
from ffelab.ledger import DEFAULT_LABELS

OUT = Path(__file__).resolve().parents[1] / "datasets"

# (func, gold, asm, callers, callees, data_refs)
FUNCS = [
    (9001, "platform", """push ebp
mov ebp,esp
mov dx,0x3c8
mov al,[ebp+8]
out dx,al
inc dx
mov al,[ebp+12]
out dx,al
mov al,[ebp+16]
out dx,al
mov al,[ebp+20]
out dx,al
pop ebp
ret""", [9020, 9021], [], []),
    (9002, "io", """push ebp
mov ebp,esp
push ebx
mov ah,0x3d
mov al,0
mov edx,[ebp+8]
int 0x21
jc .fail
mov ebx,eax
mov ah,0x3f
mov ecx,[ebp+16]
mov edx,[ebp+12]
int 0x21
mov ah,0x3e
int 0x21
mov eax,1
jmp .done
.fail:
xor eax,eax
.done:
pop ebx
pop ebp
ret""", [9030], [], [41]),
    (9003, "render", """push ebp
mov ebp,esp
push edi
mov edi,[_DATA_named]
mov eax,[ebp+8]
mov ah,al
mov ecx,eax
shl eax,16
mov ax,cx
mov ecx,16000
rep stosd
pop edi
pop ebp
ret""", [9010, 9011, 9012], [], [52]),
    (9004, "math", """push ebp
mov ebp,esp
mov eax,[ebp+8]
imul dword [ebp+8]
mov ecx,eax
mov eax,[ebp+12]
imul dword [ebp+12]
add ecx,eax
mov eax,[ebp+16]
imul dword [ebp+16]
add eax,ecx
push eax
call FUNC_009005
add esp,4
pop ebp
ret""", [9040, 9041, 9042, 9043, 9044, 9045], [9005], []),
    (9005, "math", """push ebp
mov ebp,esp
push ebx
mov ecx,[ebp+8]
xor eax,eax
mov ebx,0x40000000
.loop:
mov edx,eax
add edx,ebx
shr eax,1
cmp ecx,edx
jb .skip
sub ecx,edx
add eax,ebx
.skip:
shr ebx,2
jnz .loop
pop ebx
pop ebp
ret""", [9004, 9046, 9047], [], []),
    (9006, "text", """push ebp
mov ebp,esp
push esi
push edi
mov esi,[ebp+8]
mov edi,[ebp+12]
.next:
lodsb
cmp al,0x61
jb .copy
cmp al,0x7a
ja .copy
sub al,0x20
.copy:
stosb
test al,al
jnz .next
pop edi
pop esi
pop ebp
ret""", [9050, 9051], [], []),
    (9007, "physics", """push ebp
mov ebp,esp
push esi
mov esi,[ebp+8]
mov eax,[esi+0x40]
mov edx,[esi+0x44]
add [esi+0x10],eax
adc [esi+0x14],edx
mov eax,[esi+0x48]
mov edx,[esi+0x4c]
add [esi+0x18],eax
adc [esi+0x1c],edx
mov eax,[esi+0x50]
mov edx,[esi+0x54]
add [esi+0x20],eax
adc [esi+0x24],edx
pop esi
pop ebp
ret""", [9060], [], []),
    (9008, "economy", """push ebp
mov ebp,esp
mov ecx,[ebp+8]
mov eax,[_DATA_named+ecx*4]
mov edx,[ebp+12]
imul eax,edx
mov ecx,100
cdq
idiv ecx
mov edx,[_DATA_named]
cmp edx,eax
jl .poor
sub [_DATA_named],eax
mov ecx,[ebp+8]
add [_DATA_named+ecx*4],1
mov eax,1
pop ebp
ret
.poor:
xor eax,eax
pop ebp
ret""", [9070], [], [60, 61, 62]),
    (9009, "audio", """push ebp
mov ebp,esp
mov dx,0x22c
.w1:
in al,dx
test al,0x80
jnz .w1
mov al,0x40
out dx,al
.w2:
in al,dx
test al,0x80
jnz .w2
mov al,[ebp+8]
out dx,al
pop ebp
ret""", [9080], [], []),
    (9010, "ui", """push ebp
mov ebp,esp
push 0
push 200
push 320
push 0
call FUNC_009003
add esp,16
mov eax,[_DATA_named]
mov ecx,[_DATA_named+4]
cmp eax,ecx
jge .done
push dword [_DATA_named+8]
push eax
call FUNC_009090
add esp,8
.done:
pop ebp
ret""", [9091], [9003, 9090], [70]),
    (9011, "sysgen", """push ebp
mov ebp,esp
mov eax,[ebp+8]
mov edx,[ebp+12]
rol eax,3
xor eax,edx
ror edx,5
add edx,eax
mov [_DATA_named],eax
mov [_DATA_named+4],edx
and eax,7
mov ecx,[_DATA_named+eax*4]
mov eax,ecx
pop ebp
ret""", [9100, 9101], [], [80, 81]),
    (9012, "ai", """push ebp
mov ebp,esp
push esi
mov esi,[ebp+8]
mov eax,[esi+0x80]
test eax,eax
jz .idle
push eax
push esi
call FUNC_009120
add esp,8
cmp eax,0x1000
jg .chase
mov byte [esi+0x90],3
jmp .done
.chase:
mov byte [esi+0x90],1
jmp .done
.idle:
mov byte [esi+0x90],0
.done:
pop esi
pop ebp
ret""", [9121], [9120], []),
    (9013, "mission", """push ebp
mov ebp,esp
mov ecx,[ebp+8]
imul ecx,ecx,0x30
mov eax,[_DATA_named+ecx+8]
cmp eax,[_DATA_named]
jg .open
mov byte [_DATA_named+ecx],2
mov eax,[_DATA_named+ecx+12]
add [_DATA_named+4],eax
push ecx
call FUNC_009130
add esp,4
.open:
pop ebp
ret""", [9131], [9130], [90, 91]),
    (9014, "unknown", """push ebp
mov ebp,esp
mov eax,[ebp+8]
mov [_DATA_named],eax
pop ebp
ret""", [9140], [], [95]),
]

# red-team targets: (func, claimed label, confidence) — some right, some wrong
CLAIMS = [
    (9001, "platform", 0.8), (9003, "ui", 0.85), (9005, "math", 0.9), (9006, "ui", 0.75),
    (9007, "ai", 0.8), (9008, "economy", 0.7), (9009, "platform", 0.8), (9011, "sysgen", 0.7),
    (9012, "physics", 0.75), (9013, "mission", 0.85),
]


def view(func, asm, callers, callees, data_refs):
    lines = asm.splitlines()
    return {"func": func, "pos": hex(0x10000 + 16 * func), "instructions": len(lines),
            "has_frame": lines[:2] == ["push ebp", "mov ebp,esp"], "aligned4": True,
            "callers": callers, "callees": callees, "data_refs": data_refs, "segment": None,
            "asm": asm, "inherited_name": None}


def main():
    blind, red = [], []
    by_func = {}
    for func, gold, asm, callers, callees, refs in FUNCS:
        v = view(func, asm, callers, callees, refs)
        by_func[func] = (gold, v)
        blind.append(EvalCase(id=f"smoke-blind-{func}", task="blind_label", func=func, view=v, gold=gold,
                              provenance="synthetic", split=split_for(f"func-{func}"),
                              note="synthetic smoke case; gold by construction"))
    for func, claimed, conf in CLAIMS:
        gold, v = by_func[func]
        red.append(EvalCase(id=f"smoke-redteam-{func}", task="red_team", func=func, view=v, gold=gold,
                            claim=TargetClaim(label=claimed, confidence=conf,
                                              suggested_test=pre_experiment_test(claimed, DEFAULT_LABELS)),
                            provenance="synthetic", split=split_for(f"func-{func}"),
                            note="synthetic smoke case; gold by construction"))
    save(blind, OUT / "smoke_blind.jsonl")
    save(red, OUT / "smoke_redteam.jsonl")
    print(f"{len(blind)} blind cases, {len(red)} red-team cases -> {OUT}")


if __name__ == "__main__":
    main()
