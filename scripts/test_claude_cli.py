import pexpect
import sys
import time

def main():
    print("Starting Claude CLI test...")
    # Spawn claude
    # We use a large timeout because LLMs can take time to think
    child = pexpect.spawn('claude', encoding='utf-8', timeout=60)
    child.logfile = sys.stdout  # Log output to stdout for debugging
    
    try:
        # Wait for the prompt. Claude CLI might use '>' or something similar.
        # We will match a generic prompt indicator like '>' or '$' or '#'
        print("Waiting for prompt...")
        # Claude Code prompt is usually "❯ " or "> "
        index = child.expect([r'❯', r'>', pexpect.TIMEOUT, pexpect.EOF], timeout=15)
        if index == 2:
            print("Timeout waiting for initial prompt.")
            return
        elif index == 3:
            print("EOF reached.")
            return
            
        print("Prompt received. Sending instruction...")
        instruction = "Create a file named hello_claude_test.py in the current directory that just prints 'Hello from automated Claude!'. Once you are done, output exactly the string '[TASK_COMPLETED]'."
        # Claude Code TUI might require a carriage return (\r) rather than OS newline to submit
        child.send(instruction + '\r')
        
        # Wait for the next prompt which means Claude has finished processing the command
        while True:
            idx = child.expect([
                r'❯', 
                r'\[y/n\]', 
                r'\(y/n\)', 
                r'Press Enter',
                pexpect.TIMEOUT, 
                pexpect.EOF
            ], timeout=120)
            
            if idx in [0, 1]:
                print("\n\nTask completed successfully! Prompt returned.")
                break
            elif idx in [2, 3]:
                print("\n\nAnswering Yes to prompt...")
                child.sendline('y')
            elif idx == 4:
                print("\n\nPressing Enter...")
                child.sendline('')
            elif idx == 5:
                print("\n\nTimeout while waiting for response. Checking buffer...")
                print(child.before)
                break
            elif idx == 6:
                print("\n\nEOF reached.")
                break
                
        # Send exit command if needed
        child.sendline('/exit')
        # We don't need to check EOF as it might hang, just close it
        child.close()
        
    except pexpect.ExceptionPexpect as e:
        print(f"Exception occurred: {e}")
        
    print("Test finished.")

if __name__ == '__main__':
    main()
