
# Overview

What this system does is generate advisories according to the provided designs. 

It consists of 2 ChatGPT Projects, One for content generation, the other for graphic generation. Both modules communicate through an agreed "handshake" and the project memory is set to be project only.

It can generate a professional looking advisory in under a minute. From information gathering to graphics, it works end to end


---

# How to use

Firstly,[](Advisories/HOW_TO_USE.md.md#Set%20up|%20set%20up) the system. Then follow the steps

1. In content generation, enter "New topic, {Topic's name}"
2. It will reply with a human readable content table, read through it, if any changes needed, mention them now
3. Once approved, enter "Approved"
4. It should spit out structured content, Copy it

5. Open the graphics generation and paste in the content.
6. It should reply with a 1 line brief of the structure. Make any changes you want to the structure.
7. Once done, enter "Approved"
8. It should generate the advisory now

## Tips
- If any news is missing in the content, you can copy a relevant article along side the first message
- If graphics start skewing from desired output. wipe the graphics generation projects chats and start anew

---

# Set up

To set it up, make 2 ChatGPT projects. Name them accordingly, and set the memory to be project only.

| Project 1                                        | Project 2                                                              |
| ------------------------------------------------ | ---------------------------------------------------------------------- |
| Content_Generation                               | Graphics_Generation                                                    |
| [Instructions](Content_Generation_instruction%5C) | [Instructions](Graphic_Generation_instruction%5C)                       |
| **Source files:**<br><br>          None          | **Source files:**<br>1. Design Booklet<br>2. Example 1<br>3. Example 2 |
